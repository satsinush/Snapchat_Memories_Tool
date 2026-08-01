import os
import sys
import json
import shutil
import subprocess
from datetime import datetime
import pytz
from tzlocal import get_localzone_name
from timezonefinder import TimezoneFinder
from pathlib import Path
from PIL import Image
from typing import Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# Load metadata
with open("input/memories_history.json", "r", encoding="utf-8") as f:
    metadata = json.load(f)["Saved Media"]

tf = TimezoneFinder()
system_timezone = get_localzone_name()

# Load chat history metadata if available
chat_metadata_map = {}
chat_history_path = Path("input/chat_history.json")
if chat_history_path.exists():
    try:
        with open(chat_history_path, "r", encoding="utf-8") as f:
            chat_data = json.load(f)
        for conv_key, messages in chat_data.items():
            for msg in messages:
                media_id_str = msg.get("Media IDs", "").strip()
                if media_id_str:
                    for mid in media_id_str.split(","):
                        mid = mid.strip()
                        if mid:
                            chat_metadata_map[mid] = {
                                "Created": msg.get("Created"),
                                "From": msg.get("From"),
                                "IsSender": msg.get("IsSender"),
                                "Title": msg.get("Conversation Title"),
                            }
    except Exception as e:
        print(f"Warning: Could not load chat_history.json: {e}")

_filename_to_meta_cache = {}


def _build_metadata_cache():
    global _filename_to_meta_cache
    if _filename_to_meta_cache:
        return

    from collections import defaultdict

    mid_map = {}
    meta_by_date = defaultdict(list)

    for m in metadata:
        link = m.get("Download Link", "") or m.get("Media Download Url", "")
        if "mid=" in link:
            mid = link.split("mid=")[1].split("&")[0]
            mid_map[mid] = m
        date_str = m.get("Date", "").split(" ")[0]
        if date_str:
            meta_by_date[date_str].append(m)

    mem_dir = Path("input/memories")
    if not mem_dir.exists():
        return

    all_main_files = sorted([f for f in mem_dir.iterdir() if "-main" in f.name])

    for f in all_main_files:
        matched = None
        for mid, m in mid_map.items():
            if mid in f.name:
                matched = m
                break

        if not matched:
            date_str = f.name.split("_")[0]
            same_date_files = [x for x in all_main_files if x.name.startswith(date_str)]
            same_date_meta = meta_by_date.get(date_str, [])

            if len(same_date_meta) == 1:
                matched = same_date_meta[0]
            elif len(same_date_meta) > 1:
                try:
                    idx = same_date_files.index(f)
                    matched = same_date_meta[idx if idx < len(same_date_meta) else 0]
                except ValueError:
                    matched = same_date_meta[0]

        if matched:
            _filename_to_meta_cache[f.name] = matched


def get_metadata(filename):
    _build_metadata_cache()
    if filename in _filename_to_meta_cache:
        return _filename_to_meta_cache[filename]

    for m in metadata:
        link = m.get("Download Link", "") or m.get("Media Download Url", "")
        if "mid=" in link:
            mid = link.split("mid=")[1].split("&")[0]
            if mid in filename:
                return m

    date_str = filename.split("_")[0]
    for m in metadata:
        if m.get("Date", "").startswith(date_str):
            return m

    return None



def adjust_time(utc_time, gps_coords, target_tz=None):
    try:
        lat, lon = map(float, gps_coords.split(", "))
        utc_dt = datetime.strptime(utc_time, "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=pytz.utc
        )
        if (lat, lon) == (0.0, 0.0):
            tz_obj = pytz.timezone(target_tz) if target_tz else pytz.utc
            return utc_dt.astimezone(tz_obj).strftime("%Y:%m:%d %H:%M:%S"), None
        gps_tz = tf.timezone_at(lng=lon, lat=lat)
        local_tz = pytz.timezone(gps_tz)
        tz_obj = pytz.timezone(target_tz) if target_tz else local_tz
        return utc_dt.astimezone(tz_obj).strftime("%Y:%m:%d %H:%M:%S"), tz_obj.zone
    except:
        dt = datetime.strptime(utc_time, "%Y-%m-%d %H:%M:%S UTC")
        return dt.strftime("%Y:%m:%d %H:%M:%S"), None


def format_dms(lat, lon):
    def dms(deg):
        d = int(deg)
        m = int((abs(deg) - abs(d)) * 60)
        s = (abs(deg) - abs(d) - m / 60) * 3600
        return f"{abs(d)} deg {m}' {s:.2f}\""

    lat_dms = f"{dms(float(lat))} N" if float(lat) >= 0 else f"{dms(float(lat))} S"
    lon_dms = f"{dms(float(lon))} E" if float(lon) >= 0 else f"{dms(float(lon))} W"
    return f"{lat_dms}, {lon_dms}"


class ExifToolRunner:
    def __init__(self):
        self.proc = None

    def _start(self):
        if self.proc is None or self.proc.poll() is not None:
            try:
                self.proc = subprocess.Popen(
                    ["exiftool", "-stay_open", "True", "-@", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except Exception:
                self.proc = None

    def execute(self, args):
        self._start()
        if not self.proc:
            subprocess.run(["exiftool"] + args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        try:
            for arg in args:
                self.proc.stdin.write(f"{arg}\n")
            self.proc.stdin.write("-execute\n")
            self.proc.stdin.flush()

            while True:
                line = self.proc.stdout.readline()
                if not line or "{ready}" in line:
                    break
        except Exception:
            self.proc = None
            subprocess.run(["exiftool"] + args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("-stay_open\nFalse\n-execute\n")
                self.proc.stdin.flush()
                self.proc.communicate(timeout=2)
            except Exception:
                pass
            self.proc = None


_exiftool_runner = ExifToolRunner()


def update_metadata(file_path, date_time, gps_coords=None, only_modified=False):
    current_time = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
    cmd = ["exiftool", "-overwrite_original"]

    if not only_modified:
        cmd += [
            "-tagsFromFile",
            "@",
            "-All:Time*=",
            "-AllDates=",
            "-MediaCreateDate=",
            "-MediaModifyDate=",
            "-CreateDate=",
            "-ModifyDate=",
            "-TrackCreateDate=",
            "-TrackModifyDate=",
            "-QuickTime:CreateDate=",
            "-QuickTime:ModifyDate=",
            "-UserData:DateTimeOriginal=",
            "-XMP:DateTimeOriginal=",
            "-XMP:CreateDate=",
            "-XMP:ModifyDate=",
            "-XMP-exif:DateTimeOriginal=",
            "-XMP-pdf:CreationDate=",
            "-DateTimeOriginal=",
            "-DateCreated=",
            "-DateTimeDigitized=",
            "-XPKeywords=",
            "-XPComment=",
            "-XPSubject=",
            "-XPTitle=",
            "-Microsoft:DateAcquired=",
        ]

    cmd += [
        f"-FileCreateDate={current_time}",
        f"-FileModifyDate={current_time}",
    ]

    if not only_modified:
        cmd += [
            f"-AllDates={date_time}",
            f"-MediaCreateDate={date_time}",
            f"-MediaModifyDate={date_time}",
            f"-CreateDate={date_time}",
            f"-ModifyDate={date_time}",
            f"-TrackCreateDate={date_time}",
            f"-TrackModifyDate={date_time}",
            f"-QuickTime:CreateDate={date_time}",
            f"-QuickTime:ModifyDate={date_time}",
            f"-UserData:DateTimeOriginal={date_time}",
            f"-XMP:CreateDate={date_time}",
            f"-XMP:ModifyDate={date_time}",
            f"-XMP:DateCreated={date_time}",
            f"-XMP-exif:DateTimeOriginal={date_time}",
            f"-XMP-pdf:CreationDate={date_time}",
            f"-DateTimeOriginal={date_time}",
            f"-DateTimeDigitized={date_time}",
            f"-Microsoft:DateAcquired={date_time}",
            "-Keywords=Snapchat",
            "-XPKeywords=Snapchat",
            "-Subject=Snapchat",
            "-XMP-dc:Subject=Snapchat",
            "-Keys:Keywords=Snapchat",
            "-UserData:Keywords=Snapchat",
            "-ItemList:Keyword=Snapchat",
        ]
        if gps_coords and gps_coords != "0.0, 0.0":
            lat, lon = gps_coords.split(", ")
            dms = format_dms(float(lat), float(lon))
            cmd.extend(
                [
                    f"-GPSLatitude={lat}",
                    f"-GPSLongitude={lon}",
                    "-GPSLatitudeRef=N" if float(lat) > 0 else "-GPSLatitudeRef=S",
                    "-GPSLongitudeRef=E" if float(lon) > 0 else "-GPSLongitudeRef=W",
                    f"-XMP-exif:GPSLatitude={lat}",
                    f"-XMP-exif:GPSLongitude={lon}",
                    f"-Keys:GPSCoordinates={dms}",
                ]
            )

    cmd.append(str(file_path))
    _exiftool_runner.execute(cmd[1:])

    try:
        timestamp = datetime.strptime(date_time, "%Y:%m:%d %H:%M:%S").timestamp()
        os.utime(file_path, (timestamp, timestamp))
    except:
        pass


def generate_filename(date_time_str):
    dt_obj = datetime.strptime(date_time_str, "%Y:%m:%d %H:%M:%S")
    return dt_obj.strftime("%Y-%m-%d_%H-%M-%S")


def apply_overlay_image(base_path, overlay_path, output_path):
    try:
        base = Image.open(base_path).convert("RGBA")
        overlay = Image.open(overlay_path).convert("RGBA")
        overlay = overlay.resize(base.size, Image.LANCZOS)
        base.paste(overlay, (0, 0), overlay)
        base.convert("RGB").save(output_path, "JPEG", quality=95)
        return True
    except Exception as e:
        print(f"   Overlay image failed for {base_path.name}: {e}")
        return False


def apply_overlay_video(base_path, overlay_path, output_path):
    try:
        # Get video resolution
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(base_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True)

        lines = result.stdout.strip().splitlines()
        if not lines:
            return False

        parts = [p for p in lines[0].split(",") if p.strip()]
        if len(parts) < 2:
            return False

        width = int(parts[0])
        height = int(parts[1])

        if width > height:
            return apply_overlay_landscape(base_path, overlay_path, output_path)
        else:
            return apply_overlay_portrait(base_path, overlay_path, output_path)

    except Exception:
        return False


def get_video_resolution(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    info = json.loads(result.stdout)
    width = info['streams'][0]['width']
    height = info['streams'][0]['height']
    return width, height


def apply_overlay_landscape(base_path, overlay_path, output_path):
    try:
        width, height = get_video_resolution(base_path)

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-i", str(base_path),
                "-i", str(overlay_path),
                "-filter_complex",
                f"[0:v]transpose=2[vid];[1:v]transpose=2,scale={width}:{height}[ovr];[vid][ovr]overlay=0:0",
                "-map_metadata", "-1",
                "-metadata:s:v", "rotate=0",
                "-c:v", "libx264",
                "-crf", "18",
                "-preset", "fast",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(output_path)
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return output_path.exists()

    except Exception as e:
        print(f"   Overlay video failed (landscape) for {base_path.name}: {e}")
        return False


def apply_overlay_portrait(base_path, overlay_path, output_path):
    try:
        # Get resolution again
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(base_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True)
        lines = result.stdout.strip().splitlines()
        width, height = lines[0].split(",")

        resized_overlay = Path(str(output_path).replace(".mp4", "_resized_overlay.png"))

        # Resize overlay to match video size
        resize_cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-i", str(overlay_path),
            "-vf", f"scale={width}:{height}",
            str(resized_overlay)
        ]
        subprocess.run(resize_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Apply overlay directly
        overlay_cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-i", str(base_path),
            "-i", str(resized_overlay),
            "-filter_complex", "overlay=0:0",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path)
        ]
        subprocess.run(overlay_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if resized_overlay.exists():
            os.remove(resized_overlay)

        return output_path.exists()

    except Exception as e:
        print(f"   Overlay video failed (portrait) for {base_path.name}: {e}")
        return False


def merge_video_clips(groups, input_dir, output_dir_location, output_dir_system):
    used_filenames = {}

    for group in groups:
        first_clip = group[0]
        first_meta = get_metadata(first_clip.name)
        date_utc = first_meta["Date"]
        gps_coords = None
        if (
            "Location" in first_meta
            and "Latitude, Longitude: " in first_meta["Location"]
        ):
            gps_coords = first_meta["Location"].split(": ")[1]

        if gps_coords and gps_coords != "0.0, 0.0":
            gps_local_str, gps_tz = adjust_time(date_utc, gps_coords)
        else:
            dt = datetime.strptime(date_utc, "%Y-%m-%d %H:%M:%S UTC")
            gps_local_str = (
                pytz.utc.localize(dt)
                .astimezone(pytz.timezone(system_timezone))
                .strftime("%Y:%m:%d %H:%M:%S")
            )
            gps_tz = None

        dt = datetime.strptime(date_utc, "%Y-%m-%d %H:%M:%S UTC")
        system_time_str = pytz.utc.localize(dt).astimezone(pytz.timezone(system_timezone)).strftime("%Y:%m:%d %H:%M:%S")

        base_filename = generate_filename(gps_local_str)
        count = used_filenames.get(base_filename, 0) + 1
        used_filenames[base_filename] = count
        filename = base_filename if count == 1 else f"{base_filename}_{count}"

        merged_path_location = output_dir_location / f"{filename}.mp4"
        merged_path_system = output_dir_system / f"{filename}.mp4"

        concat_list = Path("temp_inputs.txt")
        with open(concat_list, "w") as f:
            for clip in group:
                f.write(f"file '{clip.as_posix()}'\n")

        print(f"\n→  Merging videos (location, date, and time match): {[clip.name for clip in group]}")

        if gps_coords and gps_coords != "0.0, 0.0":
            print(f"   Location → ({gps_coords})")
            if gps_tz:
                print(f"   Timezone used → {gps_tz}")
        else:
            print("   Location → none found")
            print(f"   System timezone used → {system_timezone}")
        print(f"   Final datetime → {gps_local_str}")

        subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(merged_path_location)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        update_metadata(merged_path_location, gps_local_str, gps_coords)
        print(f"   File name updated → {filename}.mp4")
        print(f"   Added to → memories location time")

        overlay_name = first_clip.stem.split("-main")[0] + "-overlay.png"
        overlay_path = input_dir / overlay_name

        if overlay_path.exists():
            overlay_output_location = output_dir_location / f"{filename}_overlay.mp4"
            if apply_overlay_video(merged_path_location, overlay_path, overlay_output_location):
                update_metadata(overlay_output_location, gps_local_str, gps_coords)
                print(f"   Overlay version added → {overlay_output_location.name}")

        subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(merged_path_system)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        update_metadata(merged_path_system, system_time_str, gps_coords)
        print(f"\n→  Processing copy {filename}.mp4")
        print(f"   System timezone used → {system_timezone}")
        print(f"   Final datetime → {system_time_str}")
        print(f"   Added to → memories system time")

        concat_list.unlink()

        if overlay_path.exists():
            overlay_output_system = output_dir_system / f"{filename}_overlay.mp4"
            if apply_overlay_video(merged_path_system, overlay_path, overlay_output_system):
                update_metadata(overlay_output_system, system_time_str, gps_coords)
                print(f"   Overlay version added → {overlay_output_system.name}")

    return used_filenames


def process_memories(): 
    input_dir = Path("input/memories")
    output_dir_mem = Path("output/memories location time")
    output_dir_system = Path("output/memories system time")
    output_dir_mem.mkdir(parents=True, exist_ok=True)
    output_dir_system.mkdir(parents=True, exist_ok=True)

    all_videos = []
    for file in sorted(input_dir.iterdir()):
        if file.suffix.lower() == ".mp4" and "-main" in file.name:
            meta = get_metadata(file.name)
            if meta:
                all_videos.append((file, meta))

    all_videos.sort(key=lambda x: x[1]["Date"])
    groups = []
    current_group = []

    for i, (file, meta) in enumerate(all_videos):
        if not current_group:
            current_group.append(file)
            continue
        prev_meta = get_metadata(current_group[-1].name)
        prev_dt = datetime.strptime(prev_meta["Date"], "%Y-%m-%d %H:%M:%S UTC")
        curr_dt = datetime.strptime(meta["Date"], "%Y-%m-%d %H:%M:%S UTC")
        time_diff = abs((curr_dt - prev_dt).total_seconds())
        same_gps = (
            "Location" in meta
            and "Location" in prev_meta
            and meta["Location"] == prev_meta["Location"]
        )
        if same_gps and 9 <= time_diff <= 11:
            current_group.append(file)
        else:
            groups.append(current_group)
            current_group = [file]
    if current_group:
        groups.append(current_group)

    used_filenames = merge_video_clips(
        [g for g in groups if len(g) > 1], input_dir, output_dir_mem, output_dir_system
    )

    to_keep = [g[0] for g in groups if len(g) == 1]
    existing_filenames = used_filenames.copy()

    for file in sorted(input_dir.iterdir()):
        if "-main" not in file.name:
            continue
        if file.suffix.lower() == ".mp4" and file not in to_keep:
            continue

        meta = get_metadata(file.name)
        if not meta:
            continue

        date_utc = meta["Date"]
        gps_coords = None
        if "Location" in meta and "Latitude, Longitude: " in meta["Location"]:
            gps_coords = meta["Location"].split(": ")[1]

        print(f"\n→  Processing memories: {file.name}")

        # GPS-local time for memories
        if gps_coords and gps_coords != "0.0, 0.0":
            date_time, tz_used = adjust_time(date_utc, gps_coords)
            print(f"   Location → ({gps_coords})")
            print(f"   Timezone used → {tz_used}")
        else:
            dt = datetime.strptime(date_utc, "%Y-%m-%d %H:%M:%S UTC")
            date_time = (
                pytz.utc.localize(dt)
                .astimezone(pytz.timezone(system_timezone))
                .strftime("%Y:%m:%d %H:%M:%S")
            )
            print("   Location → none found")
            print(f"   System timezone used → {system_timezone}")

        ext = file.suffix.lower()
        base_filename = generate_filename(date_time)

        # Add counter if filename already used
        count = existing_filenames.get(base_filename, 0) + 1
        existing_filenames[base_filename] = count
        filename = base_filename if count == 1 else f"{base_filename}_{count}"

        out_mem = output_dir_mem / f"{filename}{ext}"
        shutil.copy2(file, out_mem)
        update_metadata(out_mem, date_time, gps_coords)

        print(f"   Final datetime → {date_time}")
        print(f"   File name updated → {filename}{ext}")
        print(f"   Added to → memories location time")

        # Apply overlay if applicable — memories
        original_stem = file.stem.replace("-main", "")
        overlay_input = input_dir / f"{original_stem}-overlay.png"

        if ext in [".jpg", ".jpeg"] and overlay_input.exists():
            overlay_out_mem = output_dir_mem / f"{filename}_overlay.jpg"
            if apply_overlay_image(out_mem, overlay_input, overlay_out_mem):
                update_metadata(overlay_out_mem, date_time, gps_coords)
                print(f"   Overlay version added → {filename}_overlay.jpg")

        elif ext == ".mp4" and overlay_input.exists():
            overlay_out_mem = output_dir_mem / f"{filename}_overlay.mp4"
            if apply_overlay_video(out_mem, overlay_input, overlay_out_mem):
                update_metadata(overlay_out_mem, date_time, gps_coords)
                print(f"   Overlay version added → {filename}_overlay.mp4")

        # ----- OUTPUT FOR MEMORIES-SYSTEM -----
        out_system = output_dir_system / f"{filename}{ext}"
        shutil.copy2(file, out_system)
        dt = datetime.strptime(date_utc, "%Y-%m-%d %H:%M:%S UTC")
        system_time = (
            pytz.utc.localize(dt)
            .astimezone(pytz.timezone(system_timezone))
            .strftime("%Y:%m:%d %H:%M:%S")
        )
        update_metadata(out_system, system_time, gps_coords)

        print(f"\n→  Processing copy {filename}{ext}")
        print(f"   System timezone used → {system_timezone}")
        print(f"   Final datetime → {system_time}")
        print(f"   Added to → memories system time")

        # Apply overlay if applicable — system
        if ext in [".jpg", ".jpeg"] and overlay_input.exists():
            overlay_out_system = output_dir_system / f"{filename}_overlay.jpg"
            if apply_overlay_image(out_system, overlay_input, overlay_out_system):
                update_metadata(overlay_out_system, system_time, gps_coords)
                print(f"   Overlay version added → {filename}_overlay.jpg")

        elif ext == ".mp4" and overlay_input.exists():
            overlay_out_system = output_dir_system / f"{filename}_overlay.mp4"
            if apply_overlay_video(out_system, overlay_input, overlay_out_system):
                update_metadata(overlay_out_system, system_time, gps_coords)
                print(f"   Overlay version added → {filename}_overlay.mp4")


# Detect if file has a video stream
def has_video_stream(file_path: Path) -> bool:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=nw=1:nk=1",
            str(file_path)
        ], capture_output=True, text=True)
        return "video" in result.stdout.strip()
    except Exception:
        return False
    

# Detect if file has an audio stream
def has_audio_stream(file_path: Path) -> bool:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=nw=1:nk=1",
            str(file_path)
        ], capture_output=True, text=True)
        return "audio" in result.stdout.strip()
    except Exception:
        return False


def convert_to_mp3(input_file: Path, output_file: Path):
    try:
        subprocess.run([
            "ffmpeg", "-y", "-nostdin", "-i", str(input_file), "-vn", "-acodec", "libmp3lame", str(output_file)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_file.exists()
    except Exception:
        return False


def get_chat_media_datetime(file_path: Path) -> tuple:
    date_str = file_path.name.split("_")[0]
    mid = ""
    parts = file_path.name.split("_", 1)
    if len(parts) > 1:
        mid = parts[1].rsplit(".", 1)[0]

    extra_info = ""

    # Priority 1: Match in chat_history.json
    if mid and mid in chat_metadata_map:
        entry = chat_metadata_map[mid]
        utc_str = entry.get("Created")
        if utc_str:
            try:
                dt_utc = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=pytz.utc
                )
                formatted = dt_utc.astimezone(
                    pytz.timezone(system_timezone)
                ).strftime("%Y:%m:%d %H:%M:%S")

                info_parts = []
                if entry.get("From"):
                    sender = "You" if entry.get("IsSender") else entry["From"]
                    info_parts.append(f"From: {sender}")
                if entry.get("Title"):
                    info_parts.append(f"Chat: {entry['Title']}")
                if info_parts:
                    extra_info = " (" + ", ".join(info_parts) + ")"

                return formatted, extra_info
            except Exception:
                pass

    # Priority 2: Fall back to file stat timestamp
    try:
        stat = file_path.stat()
        for ts in (stat.st_ctime, stat.st_mtime):
            if ts > 0:
                dt = datetime.fromtimestamp(ts)
                time_part = dt.strftime("%H:%M:%S")
                return (
                    datetime.strptime(
                        f"{date_str} {time_part}", "%Y-%m-%d %H:%M:%S"
                    ).strftime("%Y:%m:%d %H:%M:%S"),
                    extra_info,
                )
    except Exception:
        pass

    # Priority 3: Fall back to 00:00:00
    return (
        datetime.strptime(f"{date_str} 00:00:00", "%Y-%m-%d %H:%M:%S").strftime(
            "%Y:%m:%d %H:%M:%S"
        ),
        extra_info,
    )


def process_chat_media():
    input_dir = Path("input/chat_media")
    output_dir = Path("output/chat media")
    voice_dir = Path("output/chat media voice messages")
    output_dir.mkdir(parents=True, exist_ok=True)
    voice_dir.mkdir(parents=True, exist_ok=True)
    date_counter = {}
    voice_counter = {}

    for file in sorted(input_dir.iterdir()):
        # Skip unsupported file types
        if file.suffix.lower() not in [".jpg", ".jpeg", ".mp4"]:
            print(f"\n→  Skipping unsupported file type → {file.name}")
            continue

        # Skip thumbnails
        if "thumbnail" in file.name.lower():
            print(f"\n→  Skipping thumbnail file → {file.name}")
            continue

        date_str = file.name.split("_")[0]
        formatted, extra_info = get_chat_media_datetime(file)

        # Process image files
        if file.suffix.lower() in [".jpg", ".jpeg"]:
            date_counter.setdefault(date_str, 0)
            date_counter[date_str] += 1
            suffix = date_counter[date_str]

            new_name = f"{date_str}_chat_media_{suffix}{file.suffix.lower()}"
            new_file = output_dir / new_name
            shutil.copy2(file, new_file)
            update_metadata(new_file, formatted)
            print(f"\n→  Processing chat_media: {file.name}{extra_info}")
            print(f"   Final datetime → {formatted}")
            print(f"   File name updated → {new_name}")
            print(f"   Added to → chat media")

        # Process video/audio files
        elif file.suffix.lower() == ".mp4":
            has_video = has_video_stream(file)
            has_audio = has_audio_stream(file)

            if not has_video and not has_audio:
                print(f"\n→  Skipping invalid mp4 (no audio/video) → {file.name}")
                continue

            if has_video:
                date_counter.setdefault(date_str, 0)
                date_counter[date_str] += 1
                suffix = date_counter[date_str]

                new_name = f"{date_str}_chat_media_{suffix}.mp4"
                new_file = output_dir / new_name
                shutil.copy2(file, new_file)
                update_metadata(new_file, formatted)
                print(f"\n→  Processing chat_media: {file.name}{extra_info}")
                print(f"   Final datetime → {formatted}")
                print(f"   File name updated → {new_name}")
                print(f"   Added to → chat media")

            elif has_audio:
                voice_counter.setdefault(date_str, 0)
                voice_counter[date_str] += 1
                suffix = voice_counter[date_str]

                new_name = f"{date_str}_voice_message_{suffix}.mp3"
                new_file = voice_dir / new_name
                success = convert_to_mp3(file, new_file)
                if success:
                    update_metadata(new_file, formatted)
                    print(f"\n→  Converted voice message to mp3 → {file.name}{extra_info}")
                    print(f"   Final datetime → {formatted}")
                    print(f"   File name updated → {new_name}")
                    print(f"   Added to → chat media voice messages")
                else:
                    print(f"→  Failed to convert voice message → {file.name}")


def main():
    try:
        process_chat_media()
        process_memories()
    finally:
        _exiftool_runner.close()


if __name__ == "__main__":
    main()
