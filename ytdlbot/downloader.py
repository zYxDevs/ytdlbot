#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - downloader.py
# 8/14/21 16:53
#

__author__ = "Benny <benny.think@gmail.com>"

import logging
import os
import pathlib
import random
import re
import subprocess
import time
import traceback
from io import StringIO
from unittest.mock import MagicMock

import fakeredis
import ffmpeg
import ffpb
import filetype
import requests
import yt_dlp as ytdl
from tqdm import tqdm

from config import (
    AUDIO_FORMAT,
    ENABLE_ARIA2,
    ENABLE_FFMPEG,
    INSTAGRAM_SESSION,
    TG_MAX_SIZE,
    IPv6,
)
from limit import Payment
from utils import adjust_formats, apply_log_formatter, current_time, sizeof_fmt

r = fakeredis.FakeStrictRedis()
apply_log_formatter()


def edit_text(bot_msg, text: str):
    key = f"{bot_msg.chat.id}-{bot_msg.message_id}"
    # if the key exists, we shouldn't send edit message
    if not r.exists(key):
        time.sleep(random.random())
        r.set(key, "ok", ex=3)
        bot_msg.edit_text(text)


def tqdm_progress(desc, total, finished, speed="", eta=""):
    def more(title, initial):
        return f"{title} {initial}" if initial else ""

    f = StringIO()
    tqdm(
        total=total,
        initial=finished,
        file=f,
        ascii=False,
        unit_scale=True,
        ncols=30,
        bar_format="{l_bar}{bar} |{n_fmt}/{total_fmt} ",
    )
    raw_output = f.getvalue()
    tqdm_output = raw_output.split("|")
    progress = f"`[{tqdm_output[1]}]`"
    detail = tqdm_output[2].replace("[A", "")
    text = f"""
{desc}

{progress}
{detail}
{more("Speed:", speed)}
{more("ETA:", eta)}
    """
    f.close()
    return text


def remove_bash_color(text):
    return re.sub(r"\u001b|\[0;94m|\u001b\[0m|\[0;32m|\[0m|\[0;33m", "", text)


def download_hook(d: dict, bot_msg):
    if d["status"] == "downloading":
        downloaded = d.get("downloaded_bytes", 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        if total > TG_MAX_SIZE:
            raise Exception(f"Your download file size {sizeof_fmt(total)} is too large for Telegram.")

        # percent = remove_bash_color(d.get("_percent_str", "N/A"))
        speed = remove_bash_color(d.get("_speed_str", "N/A"))
        eta = remove_bash_color(d.get("_eta_str", d.get("eta")))
        text = tqdm_progress("Downloading...", total, downloaded, speed, eta)
        edit_text(bot_msg, text)
        # since we're using celery, server location may be located in different continent.
        # Therefore, we can't trigger the hook very often.
        # the key is user_id + download_link
        original_url = d["info_dict"]["original_url"]
        key = f"{bot_msg.chat.id}-{original_url}"

        r.set(key, "ok", ex=5)


def upload_hook(current, total, bot_msg):
    text = tqdm_progress("Uploading...", total, current)
    edit_text(bot_msg, text)


def convert_to_mp4(video_paths: list, bot_msg):
    default_type = ["video/x-flv", "video/webm"]
    # all_converted = []
    for path in video_paths:
        # if we can't guess file type, we assume it's video/mp4
        mime = getattr(filetype.guess(path), "mime", "video/mp4")
        if mime in default_type:
            if not can_convert_mp4(path, bot_msg.chat.id):
                logging.warning("Conversion abort for %s", bot_msg.chat.id)
                bot_msg._client.send_message(bot_msg.chat.id, "Can't convert your video. ffmpeg has been disabled.")
                break
            edit_text(bot_msg, f"{current_time()}: Converting {path.name} to mp4. Please wait.")
            new_file_path = path.with_suffix(".mp4")
            logging.info("Detected %s, converting to mp4...", mime)
            run_ffmpeg_progressbar(["ffmpeg", "-y", "-i", path, new_file_path], bot_msg)
            index = video_paths.index(path)
            video_paths[index] = new_file_path


class ProgressBar(tqdm):
    b = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot_msg = self.b

    def update(self, n=1):
        super().update(n)
        t = tqdm_progress("Converting...", self.total, self.n)
        edit_text(self.bot_msg, t)


def run_ffmpeg_progressbar(cmd_list: list, bm):
    cmd_list = cmd_list.copy()[1:]
    ProgressBar.b = bm
    ffpb.main(cmd_list, tqdm=ProgressBar)


def can_convert_mp4(video_path, uid):
    return bool(ENABLE_FFMPEG)


def ytdl_download(url: str, tempdir: str, bm, **kwargs) -> list:
    payment = Payment()
    chat_id = bm.chat.id
    hijack = kwargs.get("hijack")
    output = pathlib.Path(tempdir, "%(title).70s.%(ext)s").as_posix()
    ydl_opts = {
        "progress_hooks": [lambda d: download_hook(d, bm)],
        "outtmpl": output,
        "restrictfilenames": False,
        "quiet": True,
    }
    if ENABLE_ARIA2:
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "--min-split-size=1M",
            "--max-connection-per-server=16",
            "--max-concurrent-downloads=16",
            "--split=16",
        ]
    if url.startswith("https://drive.google.com"):
        # Always use the `source` format for Google Drive URLs.
        formats = ["source"]
    else:
        # Use the default formats for other URLs.
        formats = [
            # webm , vp9 and av01 are not streamable on telegram, so we'll extract only mp4
            "bestvideo[ext=mp4][vcodec!*=av01][vcodec!*=vp09]+bestaudio[ext=m4a]/bestvideo+bestaudio",
            "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
            None,
        ]
    adjust_formats(chat_id, url, formats, hijack)
    if download_instagram(url, tempdir):
        return list(pathlib.Path(tempdir).glob("*"))

    address = ["::", "0.0.0.0"] if IPv6 else [None]
    error = None
    video_paths = None
    for format_ in formats:
        ydl_opts["format"] = format_
        for addr in address:
            # IPv6 goes first in each format
            ydl_opts["source_address"] = addr
            try:
                logging.info("Downloading for %s with format %s", url, format_)
                with ytdl.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                video_paths = list(pathlib.Path(tempdir).glob("*"))
                break
            except Exception:
                error = traceback.format_exc()
                logging.error("Download failed for %s - %s, try another way", format_, url)
        if error is None:
            break

    if not video_paths:
        raise Exception(error)

    # convert format if necessary
    settings = payment.get_user_settings(chat_id)
    if settings[2] == "video" or isinstance(settings[2], MagicMock):
        # only convert if send type is video
        convert_to_mp4(video_paths, bm)
    if settings[2] == "audio" or hijack == "bestaudio[ext=m4a]":
        convert_audio_format(video_paths, bm)
    # split_large_video(video_paths)
    return video_paths


def convert_audio_format(video_paths: list, bm):
    # 1. file is audio, default format
    # 2. file is video, default format
    # 3. non default format

    for path in video_paths:
        streams = ffmpeg.probe(path)["streams"]
        if AUDIO_FORMAT is None and len(streams) == 1 and streams[0]["codec_type"] == "audio":
            logging.info("%s is audio, default format, no need to convert", path)
        elif AUDIO_FORMAT is None and len(streams) >= 2:
            logging.info("%s is video, default format, need to extract audio", path)
            audio_stream = next(
                (
                    stream
                    for stream in streams
                    if stream["codec_type"] == "audio"
                ),
                {"codec_name": "m4a"},
            )
            ext = audio_stream["codec_name"]
            new_path = path.with_suffix(f".{ext}")
            run_ffmpeg_progressbar(["ffmpeg", "-y", "-i", path, "-vn", "-acodec", "copy", new_path], bm)
            path.unlink()
            index = video_paths.index(path)
            video_paths[index] = new_path
        else:
            logging.info("Not default format, converting %s to %s", path, AUDIO_FORMAT)
            new_path = path.with_suffix(f".{AUDIO_FORMAT}")
            run_ffmpeg_progressbar(["ffmpeg", "-y", "-i", path, new_path], bm)
            path.unlink()
            index = video_paths.index(path)
            video_paths[index] = new_path


def split_large_video(video_paths: list):
    original_video = None
    split = False
    for original_video in video_paths:
        size = os.stat(original_video).st_size
        if size > TG_MAX_SIZE:
            split = True
            logging.warning("file is too large %s, splitting...", size)
            subprocess.check_output(f"sh split-video.sh {original_video} {TG_MAX_SIZE * 0.95} ".split())
            os.remove(original_video)

    if split and original_video:
        return list(pathlib.Path(original_video).parent.glob("*"))


def download_instagram(url: str, tempdir: str):
    if not url.startswith("https://www.instagram.com"):
        return
    logging.info("Requesting instagram download link for %s", url)
    cookies = {"sessionid": INSTAGRAM_SESSION}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }

    params = {"__a": "1", "__d": "dis"}

    try:
        data = requests.get(url, params=params, cookies=cookies, headers=headers).json()
    except Exception:
        logging.warning("Can't get instagram data for %s", url)
        return

    url_results = []
    if carousels := data["items"][0].get("carousel_media"):
        for carousel in carousels:
            if carousel.get("video_versions"):
                url_results.append(carousel["video_versions"][0]["url"])
            else:
                url_results.append(carousel["image_versions2"]["candidates"][0]["url"])

    elif video := data["items"][0].get("video_versions"):
        url_results.append(video[0]["url"])
    elif image := data["items"][0].get("image_versions2"):
        url_results.append(image["candidates"][0]["url"])
    else:
        logging.warning("Can't find any downloadable link for %s", url)
        return

    for link in url_results:
        content = requests.get(link, stream=True).content
        ext = filetype.guess_extension(content)
        save_path = pathlib.Path(tempdir, f"{id(link)}.{ext}")
        with open(save_path, "wb") as f:
            f.write(content)

    return True


    # download_instagram("https://www.instagram.com/p/CXpxSyOrWCA/", "image")
    # download_instagram("https://www.instagram.com/p/Cah_7gnDVUW/", "video")
    # download_instagram("https://www.instagram.com/p/C0ozGsjtY0W/", "reels")
    # download_instagram("https://www.instagram.com/p/C0ozPQ5o536/?img_index=1", "image carousel")
    # download_instagram("https://www.instagram.com/p/C0ozhsVo-m8/?img_index=1", "video image carousel")
