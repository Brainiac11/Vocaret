"""End-to-end daemon test: audio in -> edits out, over the real socket.

The microphone is replaced by a command that plays a WAV file in real time,
everything else (recorder plumbing, Whisper, Claude, edit resolution) is real.

Usage: python tests/daemon_flow.py utter1.wav [utter2.wav ...]
"""
import os, subprocess, sys, tempfile, threading, time

run_dir = tempfile.mkdtemp(prefix="vocaret-test-")
os.environ["VOCARET_SOCKET"] = os.path.join(run_dir, "vocaret.sock")
os.environ["XDG_STATE_HOME"] = run_dir
from vocaret import edits as E, server

daemon = subprocess.Popen([sys.executable, "-m", "vocaret", "daemon"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        if server.is_running():
            break
        time.sleep(0.1)
    assert server.is_running(), "daemon did not start"
    text, pos = "", {"line": 0, "character": 0}
    for wav in sys.argv[1:]:
        options = {"recorder_command":
                   f"ffmpeg -v error -re -i {wav} -f s16le -ar 16000 -ac 1 -",
                   "silence_seconds": 1.2}
        events = []
        stopped = threading.Event()
        def listen():
            for ev in server.request({"op": "start", "options": options}):
                events.append(ev["event"] if ev["event"] != "stopped" else "stopped:" + ev["reason"])
                if ev["event"] == "stopped":
                    stopped.set()
        t0 = time.time()
        threading.Thread(target=listen, daemon=True).start()
        assert stopped.wait(60), "recording never stopped"
        heard = time.time() - t0
        t1 = time.time()
        result = None
        for ev in server.request({"op": "finish", "options": options, "context": {
                "path": "Foo.java", "language": "java", "indent": "4 spaces",
                "text": text, "selection": {"start": pos, "end": pos}}}):
            if ev["event"] == "transcript":
                print(f'heard ({ev["stt_ms"]} ms): {ev["text"]}')
            if ev["event"] in ("result", "error"):
                result = ev
        assert result and result["event"] == "result", result
        pm = E.PositionMap(text)
        text = E.apply_edits(text, [(pm.to_index(e["start"]), pm.to_index(e["end"]), e["text"])
                                    for e in result["edits"]])
        pos = result["cursor"] or pos
        kinds = [e for e in dict.fromkeys(events)]
        print(f"events: {kinds}; spoke {heard:.1f}s, then {time.time() - t1:.1f}s to code "
              f"(claude {result['llm_ms']} ms)")
        print(text, "\n")
    print("status:", list(server.request({"op": "status"})))
finally:
    try:
        list(server.request({"op": "shutdown"}))
    except OSError:
        pass
    daemon.wait(timeout=10)
    print("daemon log tail:", open(os.path.join(run_dir, "vocaret", "daemon.log")).read()[-300:])
