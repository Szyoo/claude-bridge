"""图片上传：魔数校验 / 上限 / 发送时挂到消息 / helper 取图 / 删对话删文件 / 孤儿清理 / stream-json 输入。"""

from __future__ import annotations

import base64
import json

from claude_bridge.worker import stream_json_user_message
from test_worker import HAPPY, FakeClient, chat_job, make_worker

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def upload(web, data=PNG, name="", ctype="image/png"):
    return web.post(f"/api/files?name={name}", content=data, headers={"Content-Type": ctype})


def test_upload_sniffs_format_and_serves_bytes(web, agent, bridge):
    r = upload(web, PNG, name="截图")
    assert r.status_code == 201
    f = r.json()
    assert f["mime"] == "image/png" and f["name"] == "截图" and f["size"] == len(PNG) and "fname" not in f
    # the declared content type is ignored: a JPEG labelled png is stored as JPEG
    assert upload(web, JPEG, ctype="image/png").json()["mime"] == "image/jpeg"
    assert upload(web, WEBP).json()["mime"] == "image/webp"

    got = web.get(f"/api/files/{f['id']}")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png" and got.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in got.headers["cache-control"]
    assert agent.get(f"/api/agent/files/{f['id']}").content == PNG
    assert web.get(f"/api/agent/files/{f['id']}").status_code == 401  # helper route needs the token
    assert web.get("/api/files/nope").status_code == 404
    assert (bridge.config.files_dir / f"{f['id']}.png").read_bytes() == PNG


def test_upload_rejects_non_images_and_oversize(web, bridge):
    assert upload(web, b"<svg xmlns='http://www.w3.org/2000/svg'/>", ctype="image/svg+xml").status_code == 415
    assert upload(web, b"hello").status_code == 415
    assert upload(web, b"").status_code == 400
    bridge.config.max_file_bytes = 32
    r = upload(web, PNG)
    assert r.status_code == 413 and "上限" in r.json()["detail"]
    assert web.get("/api/settings").json()["uploads"] == {"enabled": True, "max_bytes": 32, "max_files": 10}


def test_uploads_disabled_without_files_dir(web, bridge):
    bridge.config.files_dir = None
    assert upload(web).status_code == 404
    assert web.get("/api/settings").json()["uploads"]["enabled"] is False
    r = web.post("/api/send", json={"text": "hi", "files": ["x"], "scope": "stock"})
    assert r.status_code == 404


def test_send_with_images_attaches_and_reaches_the_helper(web, agent, bridge):
    a, b = upload(web, PNG, name="长截图 1/2").json(), upload(web, JPEG, name="长截图 2/2").json()
    r = web.post("/api/send", json={"text": "", "files": [a["id"], b["id"]], "scope": "stock"})
    assert r.status_code == 201
    tid = r.json()["thread"]
    msgs = web.get(f"/api/threads/{tid}/messages").json()["items"]
    user = next(m for m in msgs if m["role"] == "user")
    assert user["content"] == "" and [f["id"] for f in user["files"]] == [a["id"], b["id"]]
    assert all(m["files"] == [] for m in msgs if m["role"] != "user")
    assert web.get(f"/api/threads/{tid}").json()["thread"]["title"] == "图片"

    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    assert [f["name"] for f in job["payload"]["files"]] == ["长截图 1/2", "长截图 2/2"]
    assert job["payload"]["files"][1]["mime"] == "image/jpeg"

    # an upload can only be sent once; unknown ids and too many images are rejected
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True})
    again = web.post("/api/send", json={"text": "再看", "files": [a["id"]], "scope": "stock"})
    assert again.status_code == 400 and "发送过" in again.json()["detail"]
    assert web.post("/api/send", json={"text": "x", "files": ["missing"], "scope": "stock"}).status_code == 400
    bridge.config.max_files_per_message = 1
    ids = [upload(web).json()["id"] for _ in range(2)]
    assert web.post("/api/send", json={"text": "x", "files": ids, "scope": "stock"}).status_code == 400


def test_snapshot_and_live_message_carry_files(bridge, web):
    f = upload(web).json()
    r = web.post("/api/send", json={"text": "看图", "files": [f["id"]], "scope": "stock"})
    snap = bridge.service.snapshot(r.json()["thread"])
    user = next(m for m in snap["messages"] if m["role"] == "user")
    assert user["files"][0]["id"] == f["id"]


def test_delete_thread_removes_files(web, bridge):
    f = upload(web).json()
    tid = web.post("/api/send", json={"text": "看图", "files": [f["id"]], "scope": "stock"}).json()["thread"]
    path = bridge.config.files_dir / f"{f['id']}.png"
    assert path.exists()
    assert web.delete(f"/api/threads/{tid}?scope=stock").status_code == 200
    assert not path.exists() and bridge.store.get_file(f["id"]) is None


def test_orphan_uploads_are_purged_on_next_upload(web, bridge):
    old = upload(web).json()
    bridge.config.orphan_seconds = -5  # everything unsent counts as stale
    new = upload(web).json()
    assert bridge.store.get_file(old["id"]) is None and not (bridge.config.files_dir / f"{old['id']}.png").exists()
    assert bridge.store.get_file(new["id"]) is not None


def test_stream_json_user_message_images_first_labels_when_several():
    one = json.loads(stream_json_user_message("看看", [{"mime": "image/png", "name": "", "data": "QQ=="}]))
    assert one["type"] == "user" and one["message"]["role"] == "user"
    assert [c["type"] for c in one["message"]["content"]] == ["image", "text"]
    assert one["message"]["content"][0]["source"] == {"type": "base64", "media_type": "image/png", "data": "QQ=="}
    two = json.loads(stream_json_user_message("", [{"mime": "image/png", "name": "长截图 1/2", "data": "QQ=="},
                                                 {"mime": "image/jpeg", "name": "", "data": "Qg=="}]))
    c = two["message"]["content"]
    assert [x["type"] for x in c] == ["text", "image", "text", "image"]  # no trailing text block when text is empty
    assert c[0]["text"] == "长截图 1/2：" and c[2]["text"] == "图 2："


class ImageClient(FakeClient):
    def __init__(self, files):
        super().__init__()
        self.files = files
        self.fetched: list[str] = []

    def get_file(self, file_id):
        self.fetched.append(file_id)
        return self.files[file_id], "image/png"


def test_run_chat_with_images_uses_stream_json_stdin(tmp_path):
    client = ImageClient({"f1": PNG})
    worker, _ = make_worker(tmp_path, HAPPY, client=client)
    job = chat_job(text="图里是什么", session_id="old", auto_context=False)
    job["payload"]["files"] = [{"id": "f1", "name": "", "mime": "image/png"}]
    worker.run_chat(job)

    assert client.fetched == ["f1"] and client.finished[0]["ok"] is True
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:3] == ["-p", "--input-format", "stream-json"] and "图里是什么" not in args
    assert args[args.index("--output-format") + 1] == "stream-json" and args[-2:] == ["--resume", "old"]
    line = json.loads((tmp_path / "stdin.txt").read_text())
    content = line["message"]["content"]
    assert content[0]["type"] == "image" and base64.b64decode(content[0]["source"]["data"]) == PNG
    assert content[-1] == {"type": "text", "text": "图里是什么"}
    started = client.posts[0]["events"][0]["data"]["text"]
    assert "+1 张图" in started


def test_run_chat_without_images_keeps_plain_argv(tmp_path):
    worker, client = make_worker(tmp_path, HAPPY)
    worker.run_chat(chat_job(auto_context=False))
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:2] == ["-p", "我有几只股票"] and "--input-format" not in args
    assert not (tmp_path / "stdin.txt").exists()


def test_image_fetch_failure_fails_the_job(tmp_path):
    class Broken(FakeClient):
        def get_file(self, file_id):
            from claude_bridge.client import BridgeClientError
            raise BridgeClientError("图片取不到 f1: HTTP 404")

    client = Broken(jobs=[{**chat_job(), "payload": {**chat_job()["payload"], "files": [{"id": "f1", "mime": "image/png"}]}}])
    worker, _ = make_worker(tmp_path, HAPPY, client=client)
    assert worker.run_once() is True
    assert client.finished[0]["ok"] is False and "图片取不到" in client.finished[0]["error"]
