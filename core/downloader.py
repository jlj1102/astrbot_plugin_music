import asyncio
import shutil
import uuid
from pathlib import Path

import aiofiles
import aiohttp

from astrbot.api import logger

from .config import PluginConfig


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
        """下载歌曲，返回保存路径（aiohttp）"""
        song_uuid = uuid.uuid4().hex
        file_path = self.songs_dir / f"{song_uuid}.mp3"
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    logger.error(f"歌曲下载失败，HTTP 状态码：{response.status}")
                    return None
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(1024):
                        await f.write(chunk)

            logger.debug(f"歌曲下载完成，保存在：{file_path}")
            return file_path

        except Exception as e:
            logger.error(f"歌曲下载失败，错误信息：{e}")
            return None

    async def download_song_curl(self, url: str) -> Path | None:
        """下载歌曲，返回保存路径（curl via tmux，用于 NodeJS 模式）"""
        song_uuid = uuid.uuid4().hex
        file_path = self.songs_dir / f"{song_uuid}.mp3"
        session_name = f"dl_{song_uuid[:8]}"
        done_flag = self.songs_dir / f"{song_uuid}.done"
        fail_flag = self.songs_dir / f"{song_uuid}.fail"

        # 3-second countdown before downloading
        for i in range(3, 0, -1):
            logger.info(f"下载倒计时：{i}s ...")
            await asyncio.sleep(1)

        # Build the shell command that runs inside tmux:
        # curl downloads the file, then writes a flag file on success/failure
        shell_cmd = (
            f"curl -L --silent --show-error --fail --max-time 120 "
            f"--output '{file_path}' '{url}' "
            f"&& touch '{done_flag}' || touch '{fail_flag}'"
        )

        tmux_cmd = [
            "tmux", "new-session", "-d",
            "-s", session_name,
            shell_cmd,
        ]

        logger.debug(f"开始下载歌曲(tmux+curl) session={session_name} URL={url}")
        try:
            # Start the tmux session
            proc = await asyncio.create_subprocess_exec(
                *tmux_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(
                    f"tmux 启动失败：{stderr.decode(errors='replace').strip()}"
                )
                return None

            # Poll for either flag file (max 120s)
            for _ in range(240):
                await asyncio.sleep(0.5)
                if done_flag.exists():
                    break
                if fail_flag.exists():
                    logger.error("curl 下载失败（fail flag）")
                    file_path.unlink(missing_ok=True)
                    return None
            else:
                logger.error("下载超时（120s），强制关闭 tmux session")
                await asyncio.create_subprocess_exec(
                    "tmux", "kill-session", "-t", session_name
                )
                file_path.unlink(missing_ok=True)
                return None

            # Cleanup flag files
            done_flag.unlink(missing_ok=True)
            fail_flag.unlink(missing_ok=True)

            actual_size = file_path.stat().st_size
            logger.debug(f"歌曲下载完成，大小：{actual_size} 字节，路径：{file_path}")

            if actual_size < 10 * 1024:
                logger.warning(
                    f"下载文件过小（{actual_size} 字节），可能不是完整音频，已丢弃"
                )
                file_path.unlink(missing_ok=True)
                return None

            return file_path

        except FileNotFoundError as e:
            missing = "tmux" if "tmux" in str(e) else "curl"
            logger.error(f"{missing} 未安装，请先安装 {missing}")
            file_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            logger.error(f"歌曲下载失败，错误信息：{e}")
            file_path.unlink(missing_ok=True)
            return None