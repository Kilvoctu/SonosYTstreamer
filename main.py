import glob
import json
import logging
import os
import socket
import subprocess
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue
from threading import Thread

import yt_dlp
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, Response
from soco import SoCo

# config
load_dotenv()
sonos_ip = os.getenv("SONOS_IP")  # coordinator speaker
stream_port = int(os.getenv("STREAM_PORT", 8002))
web_port = int(os.getenv("WEB_PORT", 8001))
ffmpeg_dir = "ffmpeg"

# globals
url_scheme = "http"
ffmpeg_process = None
audio_url = None
stream_state = "idle"  # idle / buffering / streaming
play_queue = Queue()
current_title = ""

# Logging cleanup
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Download and find FFmpeg
def find_ffmpeg(base_dir=ffmpeg_dir):
    matches = glob.glob(os.path.join(base_dir, "**", "ffmpeg.exe"), recursive=True)
    return matches[0] if matches else None

ffmpeg_path = find_ffmpeg()
if not ffmpeg_path:
    print("Downloading FFmpeg...")
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    dest = "ffmpeg.zip"
    urllib.request.urlretrieve(url, dest)
    with zipfile.ZipFile(dest, 'r') as zip_ref:
        zip_ref.extractall(ffmpeg_dir)
    os.remove(dest)
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        raise FileNotFoundError("Could not find ffmpeg.exe after extraction!")

print("FFmpeg path:", ffmpeg_path)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

local_ip = get_local_ip()

# HTTP server for streaming
class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_stream()

    def do_HEAD(self):
        self.handle_stream(head_only=True)

    def handle_stream(self, head_only=False):
        if self.path != '/stream.mp3':
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.end_headers()

        if head_only:
            return

        global ffmpeg_process, audio_url
        if not audio_url:
            return

        ffmpeg_process = subprocess.Popen(
            [ffmpeg_path, '-re', '-i', audio_url, '-f', 'mp3', 'pipe:1'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        try:
            while ffmpeg_process:
                chunk = ffmpeg_process.stdout.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if ffmpeg_process:
                ffmpeg_process.kill()
                ffmpeg_process = None


def start_stream_server():
    server = HTTPServer(('0.0.0.0', stream_port), StreamHandler)
    server.serve_forever()

Thread(target=start_stream_server, daemon=True).start()
print(f"Streaming server running on {url_scheme}://{local_ip}:{stream_port}/stream.mp3")

app = Flask(__name__)
speaker = SoCo(sonos_ip)
coordinator = speaker.group.coordinator

def queue_runner():
    global audio_url, stream_state, current_title, ffmpeg_process
    while True:
        yt_url, title = play_queue.get()

        current_title = title
        stream_state = "buffering"

        ydl_opts = {
            'format': 'bestaudio',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info['url']

        coordinator.volume = 20
        coordinator.play_uri(f'{url_scheme}://{local_ip}:{stream_port}/stream.mp3')

        start_time = time.time()
        while True:
            state = coordinator.get_current_transport_info()['current_transport_state']
            if state == 'PLAYING':
                stream_state = "streaming"
                break
            if time.time() - start_time > 10:
                stream_state = "idle"
                break
            time.sleep(0.2)

        while coordinator.get_current_transport_info()['current_transport_state'] == 'PLAYING':
            time.sleep(1)

        stream_state = "idle"
        audio_url = None
        current_title = ""


Thread(target=queue_runner, daemon=True).start()

# Endpoints
@app.route('/')
def index():
    queue_list = list(play_queue.queue)
    return render_template("index.html", current_title=current_title, queue_list=queue_list)

@app.route('/play', methods=['POST'])
def play():
    global stream_state
    data = request.get_json()
    urls_str = data.get('url')
    if not urls_str:
        return jsonify({'status': 'error', 'msg': 'No URL provided'}), 400

    urls_str = urls_str.strip()
    urls_to_enqueue = []

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'ignoreerrors': True,
        'playlistend': None
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(urls_str, download=False)
        except yt_dlp.utils.DownloadError:
            return jsonify({'status': 'error', 'msg': 'Failed to extract URL/playlist'}), 400

        if 'entries' in info:  # playlist
            for entry in info['entries']:
                if entry and 'id' in entry:
                    urls_to_enqueue.append(f"https://www.youtube.com/watch?v={entry['id']}")
        else:
            urls_to_enqueue = [u.strip() for u in urls_str.split(',') if u.strip()]

    for yt_url in urls_to_enqueue:
        try:
            ydl_opts_video = {'format': 'bestaudio', 'quiet': True, 'no_warnings': True, 'noplaylist': True}
            with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title', 'Unknown Title')
            play_queue.put((yt_url, title))
            print(f"Added url: {title}")
        except yt_dlp.utils.DownloadError as e:
            print(f"Skipping video due to error: {yt_url}")
            print(f"  Error: {e}")
            continue
        except Exception as e:
            print(f"Unexpected error with video {yt_url}: {e}")
            continue

    return jsonify({'status': 'ok'})

@app.route('/stop', methods=['POST'])
def stop():
    global ffmpeg_process, stream_state, current_title
    if ffmpeg_process:
        ffmpeg_process.kill()
        ffmpeg_process = None
    coordinator.stop()
    stream_state = "idle"
    current_title = ""
    return jsonify({'status': 'ok'})

@app.route('/volume', methods=['POST'])
def volume():
    data = request.get_json()
    vol = int(data.get('volume', 20))
    coordinator.volume = vol
    return jsonify({'status': 'ok'})

@app.route('/status_stream')
def status_stream():
    def event_stream():
        last_state = ""
        last_title = ""
        last_queue = ""
        global stream_state, current_title
        while True:
            # Build queue titles as a list of strings assuming each item is (url, title)
            queue_titles = [t[1] for t in list(play_queue.queue)]
            queue_str = ";;".join(queue_titles)

            if (stream_state != last_state or
                current_title != last_title or
                queue_str != last_queue):

                last_state = stream_state
                last_title = current_title
                last_queue = queue_str

                data = json.dumps({
                    'state': stream_state,
                    'current': current_title,
                    'queue': queue_titles
                })
                yield f"data: {data}\n\n"

            time.sleep(0.1)

    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/remove_from_queue', methods=['POST'])
def remove_from_queue():
    data = request.get_json()
    url_index = int(data.get('index', -1))
    global play_queue

    if 0 <= url_index < play_queue.qsize():
        with play_queue.mutex:
            temp_list = list(play_queue.queue)
            temp_list.pop(url_index)
            play_queue.queue.clear()
            for item in temp_list:
                play_queue.queue.append(item)
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    print(f"Web control running on {url_scheme}://{local_ip}:{web_port}")
    app.run(host='0.0.0.0', port=web_port)
