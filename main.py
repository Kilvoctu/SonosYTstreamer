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
current_duration = 0
current_position = 0
is_live_stream = False
is_fetching = False

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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
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
    global audio_url, stream_state, current_title, ffmpeg_process, current_duration, current_position, is_live_stream
    while True:
        # Get queue item - supports (url, title) or (url, title, is_live)
        item = play_queue.get()
        if len(item) == 3:
            yt_url, title, is_live_stream = item
        else:
            yt_url, title = item
            is_live_stream = False

        current_title = title
        stream_state = "buffering"

        ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'noplaylist': True}

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(yt_url, download=False)
            audio_url = info['url']
            is_live = dict(info).get('is_live', False)  # type: ignore[arg-type]
            is_live_stream = is_live or is_live_stream
            current_duration = dict(info).get('duration', 0) if not is_live_stream else 0

        coordinator.volume = 20
        coordinator.play_uri(f'{url_scheme}://{local_ip}:{stream_port}/stream.mp3')

        buffer_start_time = time.time()
        while stream_state != "idle":
            state = coordinator.get_current_transport_info()['current_transport_state']
            if stream_state == "idle":
                break
            if state == 'PLAYING':
                stream_state = "streaming"
                play_start_time = time.time()
                current_position = 0
                break
            if time.time() - buffer_start_time > 10:
                stream_state = "idle"
                break
            time.sleep(0.1)

        while stream_state != "idle":
            state = coordinator.get_current_transport_info()['current_transport_state']
            if stream_state == "idle":
                break
            if state != 'PLAYING':
                break
            if not is_live_stream:
                current_position = time.time() - play_start_time
                if current_position > current_duration:
                    current_position = current_duration
            time.sleep(0.5)

        queue_size = play_queue.qsize()
        print(f"Playback ended. Stream state: {stream_state}, Queue size: {queue_size}")
        
        audio_url = None
        current_title = ""
        current_duration = 0
        current_position = 0
        is_live_stream = False

        if stream_state == "idle":
            if queue_size > 0:
                print("More items in queue, continuing...")
                stream_state = "buffering"
            else:
                print("Queue empty, going to wait state")
                stream_state = "idle"
                continue


Thread(target=queue_runner, daemon=True).start()

# Endpoints
@app.route('/')
def index():
    queue_list = list(play_queue.queue)
    return render_template("index.html", current_title=current_title, queue_list=queue_list)

@app.route('/play', methods=['POST'])
def play():
    global stream_state, is_fetching
    data = request.get_json()
    urls_str = data.get('url')
    if not urls_str:
        return jsonify({'status': 'error', 'msg': 'No URL provided'}), 400

    is_fetching = True
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
            is_fetching = False
            return jsonify({'status': 'error', 'msg': 'Failed to extract URL/playlist'}), 400

        if 'entries' in info:  # playlist
            for entry in info['entries']:
                if entry and 'id' in entry:
                    urls_to_enqueue.append(f"https://www.youtube.com/watch?v={entry['id']}")
        else:
            urls_to_enqueue = [u.strip() for u in urls_str.split(',') if u.strip()]

    for yt_url in urls_to_enqueue:
        try:
            ydl_opts_video = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True
            }
            with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
                info = ydl.extract_info(yt_url, download=False)
                title = info.get('title', 'Unknown Title')
                is_live = info.get('is_live', False)
            
            play_queue.put((yt_url, title, is_live))
            print(f"Added url: {title} {'[LIVE]' if is_live else ''}")
        except Exception as e:
            print(f"Unexpected error with video {yt_url}: {e}")
            continue

    is_fetching = False
    return jsonify({'status': 'ok'})

@app.route('/stop', methods=['POST'])
def stop():
    global ffmpeg_process, stream_state, current_title, is_live_stream, current_duration, current_position
    stream_state = "idle"
    current_title = ""
    is_live_stream = False
    current_duration = 0
    current_position = 0
    
    try:
        coordinator.stop()
    except (Exception,):
        pass
    
    if ffmpeg_process:
        ffmpeg_process.kill()
        ffmpeg_process = None
    
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
        last_queue_is_live = []
        last_is_live = False
        last_is_fetching = False
        last_duration = 0
        last_position = 0
        global stream_state, current_title, current_duration, current_position, is_live_stream, is_fetching
        while True:
            queue_data = []
            for t in list(play_queue.queue):
                if len(t) >= 3:
                    queue_data.append({'title': t[1], 'is_live': t[2]})
                else:
                    queue_data.append({'title': t[1], 'is_live': False})
            
            queue_titles = [q['title'] for q in queue_data]
            queue_is_live = [q['is_live'] for q in queue_data]
            queue_str = ";;".join(queue_titles)
            queue_is_live_str = ";;".join([str(x) for x in queue_is_live])

            if (stream_state != last_state or
                current_title != last_title or
                queue_str != last_queue or
                queue_is_live_str != last_queue_is_live or
                is_live_stream != last_is_live or
                is_fetching != last_is_fetching or
                int(current_duration) != int(last_duration) or
                int(current_position) != int(last_position)):

                last_state = stream_state
                last_title = current_title
                last_queue = queue_str
                last_queue_is_live = queue_is_live_str
                last_duration = current_duration
                last_position = current_position
                last_is_live = is_live_stream
                last_is_fetching = is_fetching

                data = json.dumps({
                    'state': stream_state,
                    'current': current_title,
                    'queue': queue_titles,
                    'queue_is_live': queue_is_live,
                    'duration': current_duration,
                    'position': current_position,
                    'is_live': is_live_stream,
                    'is_fetching': is_fetching
                })
                yield f"data: {data}\n\n"

            time.sleep(0.1)

    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/remove_from_queue', methods=['POST'])
def remove_from_queue():
    data = request.get_json()
    url_index = int(data.get('index', -1))
    global play_queue

    try:
        items = list(play_queue.queue)
        
        if 0 <= url_index < len(items):
            items.pop(url_index)  # Remove the item at index
            play_queue.queue.clear()
            play_queue.queue.extend(items)
        else:
            print(f"Index {url_index} invalid for {len(items)} items")
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': str(e)}), 500


if __name__ == '__main__':
    print(f"Web control running on {url_scheme}://{local_ip}:{web_port}")
    app.run(host='0.0.0.0', port=web_port)
