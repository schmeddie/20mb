#!/usr/bin/env python3
"""Tiny web UI: upload a video, get back a version under 20 MB.

Run:  python app.py   then open http://localhost:5000
"""

import os
import shutil
import tempfile
import threading
import time
import uuid

from flask import Flask, abort, jsonify, request, send_file

from compress import CompressError, compress

app = Flask(__name__)
WORKDIR = tempfile.mkdtemp(prefix="20mb_web_")
JOB_TTL = 60 * 60  # delete finished jobs after an hour
jobs = {}
lock = threading.Lock()


def cleanup_old_jobs():
    now = time.time()
    with lock:
        for job_id, job in list(jobs.items()):
            if job["status"] != "working" and now - job["created"] > JOB_TTL:
                shutil.rmtree(job["dir"], ignore_errors=True)
                del jobs[job_id]


def run_job(job_id, src, dst, target):
    job = jobs[job_id]

    def progress(p):
        job["progress"] = p

    def log(msg):
        job["log"].append(msg)

    try:
        job["size"] = compress(src, dst, target, progress, log)
        job["status"] = "done"
    except CompressError as e:
        job["status"], job["error"] = "error", str(e)
    except Exception as e:  # keep the UI from hanging on unexpected failures
        job["status"], job["error"] = "error", f"Unexpected error: {e}"
    finally:
        if os.path.exists(src):
            os.remove(src)


@app.get("/")
def index():
    return PAGE


@app.post("/upload")
def upload():
    cleanup_old_jobs()
    f = request.files.get("video")
    if f is None or not f.filename:
        return jsonify(error="No file uploaded."), 400
    try:
        target = float(request.form.get("target", 20))
    except ValueError:
        return jsonify(error="Invalid target size."), 400
    if not 1 <= target <= 1000:
        return jsonify(error="Target size must be between 1 and 1000 MB."), 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORKDIR, job_id)
    os.makedirs(job_dir)
    ext = os.path.splitext(f.filename)[1][:10] or ".mp4"
    src = os.path.join(job_dir, "input" + ext)
    f.save(src)

    stem = os.path.splitext(os.path.basename(f.filename))[0] or "video"
    jobs[job_id] = {
        "status": "working", "progress": 0.0, "log": [], "error": None, "size": None,
        "dir": job_dir, "created": time.time(),
        "out": os.path.join(job_dir, "output.mp4"),
        "name": f"{stem}_{target:g}mb.mp4",
    }
    threading.Thread(target=run_job, args=(job_id, src, jobs[job_id]["out"], target),
                     daemon=True).start()
    return jsonify(id=job_id)


@app.get("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id) or abort(404)
    return jsonify({k: job[k] for k in ("status", "progress", "log", "error", "size")})


@app.get("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id) or abort(404)
    if job["status"] != "done":
        abort(404)
    return send_file(job["out"], as_attachment=True, download_name=job["name"])


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>20MB Video Compressor</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --text:#1d1f23; --muted:#6b7079; --accent:#5865f2; --border:#dcdfe4; --err:#d83c3e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1e1f22; --card:#2b2d31; --text:#f2f3f5; --muted:#a4a8b0; --border:#3f4147; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:16px;
         background:var(--bg); color:var(--text); font:16px/1.5 system-ui, sans-serif; }
  main { width:100%; max-width:480px; background:var(--card); border-radius:12px; padding:28px; }
  h1 { margin:0 0 4px; font-size:22px; }
  p.sub { margin:0 0 20px; color:var(--muted); font-size:14px; }
  #drop { border:2px dashed var(--border); border-radius:10px; padding:32px 16px; text-align:center;
          cursor:pointer; color:var(--muted); transition:border-color .15s; }
  #drop.over, #drop:hover { border-color:var(--accent); }
  #drop strong { color:var(--text); display:block; word-break:break-all; }
  label.row { display:flex; align-items:center; gap:8px; margin:16px 0; font-size:14px; }
  input[type=number] { width:80px; padding:6px 8px; border:1px solid var(--border); border-radius:6px;
                       background:var(--bg); color:var(--text); font:inherit; }
  button, a.btn { display:block; width:100%; padding:12px; border:0; border-radius:8px; background:var(--accent);
                  color:#fff; font:600 15px system-ui, sans-serif; cursor:pointer; text-align:center; text-decoration:none; }
  button:disabled { opacity:.5; cursor:default; }
  .bar { height:8px; background:var(--border); border-radius:4px; overflow:hidden; margin:16px 0 8px; }
  .bar div { height:100%; width:0; background:var(--accent); transition:width .3s; }
  #log { font:12px/1.5 ui-monospace, monospace; color:var(--muted); white-space:pre-wrap; margin:0 0 16px; }
  .err { color:var(--err); }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<main>
  <h1>Video Compressor</h1>
  <p class="sub">Shrinks a video to fit under the size limit (lowering resolution as needed) so you can upload it to Discord.</p>

  <form id="form">
    <div id="drop"><strong id="fname">Drop a video here</strong>or click to choose a file</div>
    <input id="file" type="file" accept="video/*" hidden>
    <label class="row">Target size <input id="target" type="number" value="20" min="1" max="1000" step="1"> MB</label>
    <button id="go" disabled>Compress</button>
  </form>

  <section id="work" hidden>
    <div id="phase">Uploading…</div>
    <div class="bar"><div id="fill"></div></div>
    <pre id="log"></pre>
    <a id="dl" class="btn" hidden>Download</a>
    <p id="error" class="err" hidden></p>
    <button id="again" hidden>Compress another</button>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let file = null;

function pick(f) { if (!f) return; file = f; $("fname").textContent = f.name; $("go").disabled = false; }
$("drop").onclick = () => $("file").click();
$("file").onchange = e => pick(e.target.files[0]);
$("drop").ondragover = e => { e.preventDefault(); $("drop").classList.add("over"); };
$("drop").ondragleave = () => $("drop").classList.remove("over");
$("drop").ondrop = e => { e.preventDefault(); $("drop").classList.remove("over"); pick(e.dataTransfer.files[0]); };
$("again").onclick = () => location.reload();

function fail(msg) { $("error").textContent = msg; $("error").hidden = false; $("again").hidden = false; }

$("form").onsubmit = e => {
  e.preventDefault();
  if (!file) return;
  $("form").hidden = true; $("work").hidden = false;
  const data = new FormData();
  data.append("video", file);
  data.append("target", $("target").value);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload");
  xhr.upload.onprogress = ev => { if (ev.lengthComputable) $("fill").style.width = (ev.loaded / ev.total * 100) + "%"; };
  xhr.onerror = () => fail("Upload failed.");
  xhr.onload = () => {
    let res = {};
    try { res = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status !== 200) return fail(res.error || "Upload failed.");
    $("phase").textContent = "Compressing…"; $("fill").style.width = "0";
    poll(res.id);
  };
  xhr.send(data);
};

async function poll(id) {
  let s;
  try { s = await (await fetch("/status/" + id)).json(); }
  catch { return setTimeout(() => poll(id), 2000); }
  $("fill").style.width = (s.progress * 100) + "%";
  $("log").textContent = s.log.join("\\n");
  if (s.status === "working") return setTimeout(() => poll(id), 1000);
  if (s.status === "error") { $("phase").textContent = "Failed"; return fail(s.error); }
  $("phase").textContent = "Done — " + (s.size / 1e6).toFixed(2) + " MB";
  $("dl").href = "/download/" + id; $("dl").hidden = false; $("again").hidden = false;
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Open http://localhost:{port} in your browser")
    app.run(host="0.0.0.0", port=port, threaded=True)
