"""Authenticated chat and voice endpoints. Audio is processed in memory only."""
import io
import json
import threading
import wave

import requests
from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from .auth import operator_required
from .services.chat import LANGUAGES, NATIVE_NAMES, ProviderError, answer, pcm_audio, speech, speech_stream, stream_answer, transcribe

chat = Blueprint("chat", __name__)
_slots = threading.BoundedSemaphore(4)


@chat.before_request
def limits():
    request.max_content_length = 3 * 1024 * 1024 if request.endpoint == "chat.transcription" else 128 * 1024


@chat.errorhandler(ProviderError)
def provider_error(error):
    return jsonify(error="chat_unavailable", message=str(error)), error.status


def invalid(message):
    return jsonify(error="validation_error", message=message), 422


def json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def available_call(function, *args):
    if not _slots.acquire(blocking=False):
        raise ProviderError("All assistant sessions are busy. Please retry shortly.", 429)
    try:
        return function(*args)
    finally:
        _slots.release()


def claim_stream_slot():
    if not _slots.acquire(blocking=False):
        raise ProviderError("All assistant sessions are busy. Please retry shortly.", 429)
    released = False
    lock = threading.Lock()

    def release():
        nonlocal released
        with lock:
            if not released:
                released = True
                _slots.release()
    return release


@chat.get("/config")
@operator_required
def config():
    return jsonify(configured=bool(current_app.config.get("DEEPINFRA_API_KEY")),
                   languages=[{"code": code, "name": name, "native": NATIVE_NAMES[code]} for code, name in LANGUAGES.items()])


@chat.post("")
@operator_required
def completion():
    body = json_body()
    language = body.get("language", "en")
    if not isinstance(language, str) or language not in LANGUAGES:
        return invalid("Select a supported language.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        return invalid("Send between 1 and 20 conversation messages.")
    cleaned = []
    for index, message in enumerate(messages):
        expected = "user" if index % 2 == 0 else "assistant"
        if (not isinstance(message, dict) or message.get("role") != expected
                or not isinstance(message.get("content"), str) or not 1 <= len(message["content"].strip()) <= 8000):
            return invalid("Messages must alternate user and assistant, starting and ending with a user, with 1–8000 characters each.")
        cleaned.append({"role": expected, "content": message["content"].strip()})
    if len(cleaned) % 2 != 1 or sum(len(m["content"]) for m in cleaned) > 24000:
        return invalid("End with a user question and keep the conversation under 24000 characters.")
    if not isinstance(body.get("voice", False), bool):
        return invalid("voice must be true or false.")
    if not isinstance(body.get("stream", False), bool):
        return invalid("stream must be true or false.")
    if body.get("stream"):
        if not current_app.config.get("DEEPINFRA_API_KEY"):
            raise ProviderError("Set DEEPINFRA_API_KEY in .env and restart the server.", 503)
        release = claim_stream_slot()

        @stream_with_context
        def events():
            try:
                for event, data in stream_answer(cleaned, language, body.get("voice", False)):
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            except (ProviderError, requests.RequestException) as error:
                message = str(error) if isinstance(error, ProviderError) else "The response was interrupted. Please retry."
                yield f"event: error\ndata: {json.dumps({'message': message})}\n\n"
            finally:
                release()
        response = Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
        response.call_on_close(release)
        return response
    text, at = available_call(answer, cleaned, language, body.get("voice", False))
    return jsonify(reply=text, context_at=at)


@chat.post("/transcribe")
@operator_required
def transcription():
    language = request.form.get("language")
    if language not in LANGUAGES:
        return invalid("Select a language before recording.")
    upload = request.files.get("audio")
    if not upload:
        return invalid("Attach a WAV recording.")
    audio = upload.read(2 * 1024 * 1024 + 1)
    if len(audio) > 2 * 1024 * 1024:
        return invalid("Recordings must be no larger than 2 MiB.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as recording:
            duration = recording.getnframes() / recording.getframerate()
            if recording.getnchannels() != 1 or recording.getsampwidth() != 2 or not 0.2 <= duration <= 61:
                return invalid("Send a mono 16-bit WAV recording between 0.2 and 60 seconds.")
            if len(recording.readframes(recording.getnframes())) != recording.getnframes() * 2:
                return invalid("The WAV recording is incomplete.")
    except (wave.Error, EOFError, ZeroDivisionError):
        return invalid("The recording is not a valid WAV file.")
    return jsonify(text=available_call(transcribe, audio, language))


@chat.post("/speech")
@operator_required
def spoken_reply():
    body = json_body()
    language, text = body.get("language"), body.get("text")
    if not isinstance(language, str) or language not in LANGUAGES:
        return invalid("Select a supported language.")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 5000:
        return invalid("Spoken replies must contain 1–5000 characters.")
    if body.get("stream") is True:
        if len(text) > 600:
            return invalid("Send speech in short sentences of up to 600 characters.")
        release = claim_stream_slot()
        try:
            upstream = speech_stream(text.strip(), language)
        except Exception:
            release()
            raise

        @stream_with_context
        def audio_chunks():
            try:
                with upstream:
                    yield from pcm_audio(upstream)
            finally:
                upstream.close()
                release()
        response = Response(audio_chunks(), mimetype="application/octet-stream", headers={"X-Audio-Sample-Rate": "24000", "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
        def close_audio():
            upstream.close()
            release()
        response.call_on_close(close_audio)
        return response
    return Response(available_call(speech, text.strip(), language), mimetype="audio/mpeg")
