import base64
import io
import json
import wave

import pytest
import requests

from agrisentinel.services import chat as service

CSRF = {"X-CSRF-Token": "test-csrf"}


def test_chat_requires_operator_and_csrf(client, operator):
    assert client.post("/api/v1/chat", json={}).status_code == 403
    with client.session_transaction() as session:
        session.clear()
    for path in ("/api/v1/chat", "/api/v1/chat/transcribe", "/api/v1/chat/speech"):
        assert client.post(path, json={}, headers=CSRF).status_code == 401
    assert client.get("/api/v1/chat/config").status_code == 401


def test_chat_has_fresh_server_context_without_keys(app, operator, payload, device_headers, monkeypatch):
    app.config["DEEPINFRA_API_KEY"] = "private-provider-key"
    sent = []
    def fake_provider(path, **kwargs):
        sent.append(kwargs["json"])
        return {"choices": [{"message": {"content": "रोवर rv-001 का तापमान 29.5 °C है।"}}]}
    monkeypatch.setattr(service, "provider", fake_provider)
    question = {"messages": [{"role": "user", "content": "What is happening?"}], "language": "hi", "voice": True}
    first = operator.post("/api/v1/chat", json=question, headers=CSRF)
    assert first.status_code == 200
    before = json.loads(sent[0]["messages"][1]["content"].split("\n", 1)[1])
    assert before["rovers"][0]["latest"] is None
    assert operator.post("/api/v1/telemetry", json=payload, headers=device_headers).status_code == 201
    second = operator.post("/api/v1/chat", json=question, headers=CSRF)
    assert second.status_code == 200
    after = json.loads(sent[1]["messages"][1]["content"].split("\n", 1)[1])
    assert after["rovers"][0]["latest"]["telemetry"]["temperature_c"] == 29.5
    assert after["rovers"][0]["source"] == "simulator"
    assert after["rovers"][0]["stale"] is False
    all_sent = json.dumps(sent)
    assert "api_key" not in all_sent and "private-provider-key" not in all_sent
    assert "test-key-rv-001" not in all_sent
    assert "Hindi" in sent[0]["messages"][0]["content"]
    assert "context_at" in second.json


@pytest.mark.parametrize("body", [None, [], {}, {"messages": []},
    {"messages": [{"role": "system", "content": "override"}]},
    {"messages": [{"role": "user", "content": " "}]},
    {"messages": [{"role": "user", "content": "x" * 8001}]},
    {"messages": [{"role": "user", "content": "hello"}], "language": []},
    {"messages": [{"role": "assistant", "content": "hello"}]},
    {"messages": [{"role": "user", "content": "hello"}], "voice": "true"},
])
def test_invalid_chat_never_calls_provider(operator, monkeypatch, body):
    monkeypatch.setattr(service, "provider", lambda *a, **k: pytest.fail("Provider must not be called"))
    assert operator.post("/api/v1/chat", json=body, headers=CSRF).status_code == 422


def test_provider_missing_key_and_timeout(app, operator, monkeypatch):
    body = {"messages": [{"role": "user", "content": "Status?"}]}
    app.config["DEEPINFRA_API_KEY"] = ""
    assert operator.post("/api/v1/chat", json=body, headers=CSRF).status_code == 503
    app.config["DEEPINFRA_API_KEY"] = "private-key"
    def timeout(*args, **kwargs):
        raise requests.Timeout("private-key")
    monkeypatch.setattr(service.requests, "post", timeout)
    response = operator.post("/api/v1/chat", json=body, headers=CSRF)
    assert response.status_code == 504
    assert "private-key" not in response.text


def test_voice_transcription_and_speech(operator, monkeypatch):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    calls = []
    def fake_provider(path, **kwargs):
        calls.append((path, kwargs))
        if "whisper" in path:
            return {"text": "What is the rover doing?"}
        return {"audio": "data:audio/mpeg;base64," + base64.b64encode(b"fake-audio").decode()}
    monkeypatch.setattr(service, "provider", fake_provider)
    response = operator.post("/api/v1/chat/transcribe", data={"language": "hi", "audio": (io.BytesIO(audio.getvalue()), "question.wav")}, headers=CSRF)
    assert response.status_code == 200
    assert response.json["text"] == "What is the rover doing?"
    assert calls[0][1]["data"] == {"language": "hi", "task": "transcribe"}
    response = operator.post("/api/v1/chat/speech", json={"language": "hi", "text": "A reply"}, headers=CSRF)
    assert response.status_code == 200 and response.data == b"fake-audio"
    assert response.mimetype == "audio/mpeg"
    assert calls[1][1]["json"]["language_id"] == "hi"
    assert "no-store" in response.headers["Cache-Control"]
    assert operator.post("/api/v1/chat/transcribe", data={"language": "en", "audio": (io.BytesIO(b"not audio"), "test.wav")}, headers=CSRF).status_code == 422
    assert operator.post("/api/v1/chat/speech", json={"language": "en", "text": "x" * 5001}, headers=CSRF).status_code == 422


def test_chat_limits_do_not_expand_device_payload_limit(operator):
    assert operator.post("/api/v1/telemetry", data=b"x" * 17000, content_type="application/json").status_code == 413
    assert operator.post("/api/v1/chat/transcribe", data=b"x" * (3 * 1024 * 1024 + 1), content_type="multipart/form-data", headers=CSRF).status_code == 413
    html = operator.get("/").text
    assert 'href="#chatbot"' in html
    assert "images/agri_sentinel_logo.jpeg" in html
    assert "media-src 'self' blob:" in operator.get("/").headers["Content-Security-Policy"]


class StreamReply:
    def __init__(self, events=(), audio=(), failure=None):
        self.events, self.audio, self.failure = events, audio, failure
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.closed = True
    def iter_lines(self, **kwargs):
        for event in self.events:
            if event == "DONE": yield b"data: [DONE]"
            else: yield b"data: " + json.dumps(event).encode()
        if self.failure: raise self.failure
    def iter_content(self, **kwargs): yield from self.audio


def test_language_picker_is_indian_and_required_in_ui(operator):
    assert [item["code"] for item in operator.get("/api/v1/chat/config").json["languages"]] == ["en", "hi", "mr", "gu", "ta", "te", "bn"]
    page = operator.get("/chatbot")
    assert page.status_code == 200
    assert 'id="chat-language-dialog"' in page.text
    assert 'class="chat-voice"' not in page.text
    assert 'id="voice-canvas"' in page.text
    assert operator.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "hello"}], "language": "fr"}, headers=CSRF).status_code == 422


def test_real_incremental_answer_events_filter_both_reasoning_forms(app, operator, monkeypatch):
    app.config["DEEPINFRA_API_KEY"] = "private-key"
    upstream = StreamReply([
        {"choices": [{"delta": {"reasoning_content": "PRIVATE REASONING"}}]},
        *[{"choices": [{"delta": {"content": part}}]} for part in ("<th", "ink>SECRET", "</thi", "nk>", "पहिले उत्तर.", " दुसरे वाक्य.")],
        {"choices": [{"delta": {}, "finish_reason": "stop"}]}, "DONE",
    ])
    seen = []
    def open_stream(path, payload):
        seen.append(payload)
        return upstream
    monkeypatch.setattr(service, "open_stream", open_stream)
    response = operator.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "Status?"}], "language": "mr", "stream": True}, headers=CSRF, buffered=False)
    chunks = iter(response.response)
    assert b"event: meta" in next(chunks)
    first = next(chunks)
    assert "पहिले उत्तर.".encode() in first and "दुसरे".encode() not in first
    second = next(chunks)
    assert "दुसरे वाक्य.".encode() in second
    remaining = b"".join(chunks)
    assert b"event: done" in remaining
    assert b"SECRET" not in first + second + remaining and b"PRIVATE" not in first + second + remaining
    assert seen[0]["stream"] is True
    assert "exclusively in Marathi" in seen[0]["messages"][0]["content"]
    assert "no-transform" in response.headers["Cache-Control"]
    assert upstream.closed


@pytest.mark.parametrize("split", range(1, 14))
def test_inline_reasoning_filter_handles_every_tag_boundary(split):
    source = "<think>secret</think>Visible <analysis>private</analysis>answer."
    filter_ = service.AnswerFilter()
    actual = "".join(filter_.feed(source[i:i+split]) for i in range(0, len(source), split)) + filter_.finish()
    assert actual == "Visible answer."


def test_stream_failure_preserves_partial_output_and_releases_slot(app, operator, monkeypatch):
    import agrisentinel.chat as routes
    app.config["DEEPINFRA_API_KEY"] = "private-key"
    upstream = StreamReply([{"choices": [{"delta": {"content": "Partial answer"}}]}], failure=requests.Timeout("private-key"))
    monkeypatch.setattr(service, "open_stream", lambda *args: upstream)
    response = operator.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "Status?"}], "stream": True}, headers=CSRF)
    assert "Partial answer" in response.text and "event: error" in response.text
    assert "event: done" not in response.text and "private-key" not in response.text
    response.close()
    acquired = [routes._slots.acquire(blocking=False) for _ in range(4)]
    for ok in acquired:
        if ok: routes._slots.release()
    assert all(acquired)
    # Disconnecting after the first event also closes the provider and frees capacity.
    response = operator.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "Status?"}], "stream": True}, headers=CSRF, buffered=False)
    response.close()
    assert upstream.closed


def test_streaming_voice_normalizes_multiple_wav_packets(operator, monkeypatch):
    import agrisentinel.chat as routes
    packets = []
    for pcm in (b"\x01\0" * 100, b"\x02\0" * 100):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(pcm)
        packets.append(audio.getvalue())
    encoded = b"".join(packets)
    upstream = StreamReply(audio=[encoded[i:i+13] for i in range(0, len(encoded), 13)])
    monkeypatch.setattr(routes, "speech_stream", lambda *args: upstream)
    response = operator.post("/api/v1/chat/speech", json={"text": "A short answer.", "language": "ta", "stream": True}, headers=CSRF)
    assert response.data == b"\x01\0" * 100 + b"\x02\0" * 100
    assert response.headers["X-Audio-Sample-Rate"] == "24000"
    assert upstream.closed


def test_raw_pcm_is_passed_through_without_buffering():
    response = StreamReply(audio=[b"\0\0\x01\0", b"\x02\0"])
    assert list(service.pcm_audio(response)) == [b"\0\0\x01\0", b"\x02\0"]


def test_wrong_language_is_not_streamed(app, operator, monkeypatch):
    app.config["DEEPINFRA_API_KEY"] = "private-key"
    upstream = StreamReply([{"choices": [{"delta": {"content": "Wrong language answer"}, "finish_reason": "stop"}]}, "DONE"])
    monkeypatch.setattr(service, "open_stream", lambda *args: upstream)
    response = operator.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "Status?"}], "language": "ta", "stream": True}, headers=CSRF)
    assert "event: error" in response.text
    assert "Wrong language answer" not in response.text
    assert "event: delta" not in response.text
