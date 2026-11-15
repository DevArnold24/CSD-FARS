"""Private-copy application smoke test; launched by the protected updater only."""
from pathlib import Path
import json
import os
import re
import sys


def main():
    stage = Path(sys.argv[1]).resolve(strict=True)
    nonce = sys.argv[2]
    if not re.fullmatch('[0-9a-f]{64}', nonce):
        raise RuntimeError('A validation completion identity is required.')
    if stage != Path.cwd().resolve() or not stage.name.startswith('validation-'):
        raise RuntimeError('A private validation copy is required.')
    trusted = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(trusted))
    from scripts.client import client_setup
    client_setup.check_install(stage)
    # Import runtime libraries before placing restrictions on application code.
    import cv2
    import socket
    import sqlite3
    import subprocess
    import threading
    from dotenv import load_dotenv

    original_popen = subprocess.Popen
    def hidden_runtime_process(*args, **kwargs):
        if os.name == 'nt':
            kwargs['creationflags'] = kwargs.get('creationflags',0) | subprocess.CREATE_NO_WINDOW
        return original_popen(*args, **kwargs)
    subprocess.Popen = hidden_runtime_process
    # Load the installed face runtime before disabling process creation. Its
    # packaging helper asks Windows for the OS version using a hidden command.
    # Loading models does not open a camera or perform recognition.
    import face_recognition

    denied = []
    def refuse(*args, **kwargs):
        denied.append(True)
        raise RuntimeError('Camera, network and worker access is disabled during update validation.')

    cv2.VideoCapture = refuse
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.socket.bind = refuse
    socket.socket.sendto = refuse
    socket.create_connection = refuse
    subprocess.Popen = refuse
    original_thread_start = threading.Thread.start
    audio_threads = []
    def start_support_thread(thread):
        target = getattr(thread, '_target', None)
        if (getattr(target, '__module__', None) != 'core.audio_queue'
                or getattr(target, '__qualname__', None) != 'ThreadedAudioQueue._worker'):
            refuse()
        audio_threads.append(thread)
        return original_thread_start(thread)
    # The existing app initializes its idle audio queue on import. Exercise its
    # normal startup while refusing audio playback and all other worker starts.
    threading.Thread.start = start_support_thread
    import simpleaudio
    simpleaudio.play_buffer = refuse
    simpleaudio.WaveObject.play = refuse
    os.system = refuse
    if hasattr(os, 'startfile'):
        os.startfile = refuse

    # Reject Python-level writes and SQLite access outside the disposable copy.
    # Native libraries are trusted runtime dependencies, not sandboxed code.
    def within(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(value)).resolve().is_relative_to(stage)

    def audit(event, args):
        if event == 'open':
            name, mode, flags = args
            writing = (isinstance(mode, str) and any(flag in mode for flag in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not within(name):
                raise RuntimeError('Validation cannot write outside its private copy.')
        elif event == 'sqlite3.connect' and not within(args[0]):
            raise RuntimeError('Validation cannot open another database.')
        elif event in {'os.remove','os.rmdir','os.mkdir','os.chmod','os.utime'} and not within(args[0]):
            raise RuntimeError('Validation cannot change another folder.')
        elif event in {'os.rename','os.link','os.symlink'}:
            if not within(args[0]) or not within(args[1]):
                raise RuntimeError('Validation cannot change another folder.')
        elif event in {'subprocess.Popen','os.system','os.startfile','os.startfile/2','socket.connect','socket.bind','socket.sendto'}:
            refuse()

    sys.addaudithook(audit)
    load_dotenv(stage/'.env', override=True)
    sys.path.insert(0, str(stage))
    import config
    if Path(config.DATABASE_PATH).resolve() != stage/'database/attendance.db':
        raise RuntimeError('The application did not select the validation database.')
    import app
    app.app.config['TESTING'] = True
    with app.app.test_client() as client:
        response = client.get('/login')
        if response.status_code != 200 or b'id="login-form"' not in response.data:
            raise RuntimeError('The login page failed its update check.')
    for name in app.app.jinja_env.list_templates():
        app.app.jinja_env.get_template(name)
    from workers.session_manager import session_manager
    if session_manager.active_sessions or session_manager._camera_leases or session_manager._manager is not None:
        raise RuntimeError('The application started camera or attendance work during validation.')
    from core.audio_queue import audio_player
    audio_player.shutdown()
    if denied or any(thread.is_alive() for thread in audio_threads):
        raise RuntimeError('The application attempted background work during validation.')
    with (stage/'validation-complete.json').open('x', encoding='ascii') as stream:
        json.dump({'format':1,'nonce':nonce,'status':'passed'},stream)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    main()
