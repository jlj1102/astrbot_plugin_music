import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp

from astrbot.api import logger

from .config import PluginConfig


DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "audio/webm,audio/ogg,audio/wav,audio/*;q=0.9,application/ogg;q=0.7,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",          # avoid compressed audio streams
    "Connection": "keep-alive",
    "Range": "bytes=0-",                    # explicitly request full file
}


class Downloader:
    """下载器"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.songs_dir = self.cfg.songs_dir
        self.session = aiohttp.ClientSession(proxy=self.cfg.http_proxy)

    async def initialize(self):
        if self.cfg.clear_cache:
            self._ensure_cache_dir()

    async def close(self):
        await self.session.close()

    def _ensure_cache_dir(self) -> None:
        """重建缓存目录：存在则清空，不存在则新建"""
        if self.songs_dir.exists():
            shutil.rmtree(self.songs_dir)
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"缓存目录已重建：{self.songs_dir}")

    # CDN hostname → required Referer
    _REFERER_MAP = {
        "126.net": "https://music.163.com/",
        "163.com": "https://music.163.com/",
        "music.163.com": "https://music.163.com/",
        "qq.com": "https://y.qq.com/",
        "kugou.com": "https://www.kugou.com/",
        "kuwo.cn": "https://www.kuwo.cn/",
    }

    @classmethod
    def _origin_referer(cls, url: str) -> dict:
        """Return the correct Origin/Referer for the CDN serving this URL."""
        host = urlparse(url).netloc  # e.g. m701.music.126.net
        for domain, referer in cls._REFERER_MAP.items():
            if host.endswith(domain):
                return {"Origin": referer.rstrip("/"), "Referer": referer}
        # Generic fallback: derive from URL origin
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return {"Origin": origin, "Referer": origin + "/"}

    async def download_image(self, url: str, close_ssl: bool = True) -> bytes | None:
        """下载图片"""
        url = url.replace("https://", "http://") if close_ssl else url
        try:
            async with self.session.get(url) as response:
                img_bytes = await response.read()
                return img_bytes
        except Exception as e:
            logger.error(f"图片下载失败: {e}")

    async def download_song(self, url: str) -> Path | None:
        """下载歌曲，返回保存路径"""
        song_uuid = uuid.uuid4().hex
        file_path = self.songs_dir / f"{song_uuid}.mp3"

        headers = {**DOWNLOAD_HEADERS, **self._origin_referer(url)}

        try:
            async with self.session.get(
                url,
                headers=headers,
                allow_redirects=True,
                ssl=False,          # some CDNs use self-signed certs
            ) as response:
                if response.status not in (200, 206):
                    logger.error(
                        f"歌曲下载失败，HTTP 状态码：{response.status}，URL：{url}"
                    )
                    return None

                content_type = response.headers.get("Content-Type", "")
                content_length = response.headers.get("Content-Length")
                logger.debug(
                    f"开始下载歌曲 Content-Type={content_type} "
                    f"Content-Length={content_length} URL={url}"
                )

                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(1024 * 64):
                        await f.write(chunk)

            actual_size = file_path.stat().st_size
            logger.debug(f"歌曲下载完成，大小：{actual_size} 字节，路径：{file_path}")

            # Guard: reject suspiciously small files (likely an error page)
            if actual_size < 10 * 1024:
                logger.warning(
                    f"下载文件过小（{actual_size} 字节），可能不是完整音频，已丢弃"
                )
                file_path.unlink(missing_ok=True)
                return None

            return file_path

        except Exception as e:
            logger.error(f"歌曲下载失败，错误信息：{e}")
            file_path.unlink(missing_ok=True)
            return None