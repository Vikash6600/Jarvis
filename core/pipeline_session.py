"""
Speech-pipeline voice engine — Jarvis without Gemini Live.

When no Gemini key is configured (or the user picks VOICE ENGINE = pipeline),
main.py opens a PipelineSession instead of `client.aio.live.connect(...)`.
It deliberately imitates the handful of Live-session methods JarvisLive uses,
so playback, lip-sync, the echo guard, the transcript log and tool dispatch
all work unchanged:

    send_realtime_input(audio=Blob)   mic PCM16 @16 kHz in; an energy VAD
                                      cuts utterances out of it
    send_client_content(turns=...)    typed commands, briefings, proactive text
    send_tool_response(...)           tool results back into the conversation
    receive()                         yields Live-shaped responses:
                                      .data (PCM16 @24 kHz), .server_content
                                      (transcriptions, turn_complete), .tool_call

Speech-to-text uses the STT job (any OpenAI-compatible /audio/transcriptions
model, e.g. Groq whisper-large-v3-turbo, OpenAI whisper-1), falling back to a
local faster-whisper if it is installed. The reply comes from the CHAT job's
model with Jarvis's full tool list. Text-to-speech is Microsoft Edge's free
neural voices (edge-tts, no key), decoded in memory with miniaudio.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
import wave
from types import SimpleNamespace

import numpy as np

from core import models

IN_RATE = 16000
OUT_RATE = 24000
DEFAULT_VOICE = "en-GB-RyanNeural"      # a calm British butler
_SENT = re.compile(r"(?<=[.!?])\s+")
_MAX_HISTORY = 40


# ── helpers ──────────────────────────────────────────────────────────────────
def _to_json_schema(s):
    """Gemini-style schema (uppercase types) → JSON Schema (lowercase)."""
    if isinstance(s, dict):
        out = {}
        for k, v in s.items():
            if k in ("behavior", "scheduling", "nullable", "format", "propertyOrdering"):
                continue
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            else:
                out[k] = _to_json_schema(v)
        return out
    if isinstance(s, list):
        return [_to_json_schema(x) for x in s]
    return s


def openai_tools(decls) -> list[dict]:
    tools = []
    for d in decls or []:
        if not isinstance(d, dict):
            try:
                d = d.model_dump(mode="json", exclude_none=True)
            except Exception:
                continue
        name = d.get("name")
        if not name:
            continue
        params = _to_json_schema(d.get("parameters") or {"type": "object", "properties": {}})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        tools.append({"type": "function", "function": {
            "name": name, "description": (d.get("description") or "")[:1000], "parameters": params}})
    return tools


def _wav(pcm16: bytes, rate: int = IN_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def _resp(data=None, out_text=None, in_text=None, done=False, calls=None):
    sc = None
    if out_text is not None or in_text is not None or done:
        sc = SimpleNamespace(
            output_transcription=SimpleNamespace(text=out_text) if out_text else None,
            input_transcription=SimpleNamespace(text=in_text) if in_text else None,
            turn_complete=done, interrupted=False, model_turn=None)
    tc = SimpleNamespace(function_calls=calls) if calls else None
    return SimpleNamespace(data=data, server_content=sc, tool_call=tc,
                           session_resumption_update=None, go_away=None)


def _turn_text(turns) -> str:
    items = turns if isinstance(turns, list) else [turns]
    out = []
    for t in items:
        parts = t.get("parts") if isinstance(t, dict) else getattr(t, "parts", None)
        if parts is None and isinstance(t, str):
            out.append(t); continue
        for p in parts or []:
            txt = p.get("text") if isinstance(p, dict) else getattr(p, "text", None)
            if txt:
                out.append(txt)
    return "\n".join(out).strip()


# ── the session ──────────────────────────────────────────────────────────────
class PipelineSession:
    def __init__(self, system_prompt: str, tool_decls, voice: str = DEFAULT_VOICE, log=print):
        self._system = system_prompt
        self._tools = openai_tools(tool_decls)
        self._voice = voice or DEFAULT_VOICE
        self._log = log
        self._history: list[dict] = []
        self._out: asyncio.Queue = asyncio.Queue()
        self._busy = asyncio.Lock()
        self._pending_calls: dict[str, dict] = {}
        self._closed = False
        # VAD state
        self._buf = bytearray()
        self._speaking = False
        self._silence = 0.0
        self._voiced = 0.0
        self._noise = 300.0
        self._local_whisper = None

    async def __aenter__(self):
        if not models.candidates("chat"):
            raise RuntimeError("no chat model configured — add an API key in AI MODELS")
        return self

    async def __aexit__(self, *exc):
        self._closed = True
        return False

    # ── inputs (the Live-session surface) ─────────────────────────────────────
    async def send_realtime_input(self, audio=None, **_):
        if audio is None or self._closed:
            return
        data = getattr(audio, "data", None) or (audio.get("data") if isinstance(audio, dict) else None)
        if not data:
            return
        pcm = np.frombuffer(data, dtype=np.int16)
        if pcm.size == 0:
            return
        rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
        dur = pcm.size / IN_RATE
        start_th = max(550.0, self._noise * 3.0)
        if not self._speaking:
            self._noise = 0.95 * self._noise + 0.05 * min(rms, 3000.0)
            if rms > start_th:
                self._speaking, self._silence, self._voiced = True, 0.0, dur
                self._buf = bytearray(data)
            return
        self._buf.extend(data)
        if rms > max(400.0, self._noise * 2.0):
            self._voiced += dur
            self._silence = 0.0
        else:
            self._silence += dur
        too_long = len(self._buf) > IN_RATE * 2 * 30
        if self._silence >= 0.75 or too_long:
            utter, voiced = bytes(self._buf), self._voiced
            self._speaking, self._buf = False, bytearray()
            if voiced >= 0.3:
                asyncio.create_task(self._handle_utterance(utter))

    async def send_client_content(self, turns=None, turn_complete=True, **_):
        text = _turn_text(turns)
        if text:
            asyncio.create_task(self._handle_text(text, echo_input=False))

    async def send_tool_response(self, function_responses=None, **_):
        for fr in function_responses or []:
            fid = getattr(fr, "id", None) or (fr.get("id") if isinstance(fr, dict) else None)
            resp = getattr(fr, "response", None) or (fr.get("response") if isinstance(fr, dict) else {})
            content = resp.get("result") if isinstance(resp, dict) else resp
            self._history.append({"role": "tool", "tool_call_id": fid,
                                  "content": content if isinstance(content, str) else json.dumps(content, default=str)})
            self._pending_calls.pop(fid, None)
        if not self._pending_calls:
            asyncio.create_task(self._think())

    async def receive(self):
        while not self._closed:
            r = await self._out.get()
            yield r
            if r.server_content is not None and r.server_content.turn_complete:
                return

    # ── pipeline ──────────────────────────────────────────────────────────────
    async def _handle_utterance(self, pcm: bytes):
        text = await asyncio.get_running_loop().run_in_executor(None, self._stt, pcm)
        if not text or len(text.strip(" .?!,")) < 2:
            return
        await self._handle_text(text, echo_input=True)

    def _stt(self, pcm: bytes) -> str:
        wav = _wav(pcm)
        for prov, model in models.candidates("stt"):
            if models.cooling(prov, model):
                continue
            try:
                return models.transcribe(prov, model, wav)
            except Exception as e:
                self._log(f"[Pipeline] STT {prov.get('id')}/{model}: {str(e)[:120]}")
        try:
            if self._local_whisper is None:
                from core.stt import WhisperSTT
                self._local_whisper = WhisperSTT("base")
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            return self._local_whisper.transcribe(audio)
        except Exception as e:
            self._log(f"[Pipeline] no speech-to-text available: {e}")
            return ""

    async def _handle_text(self, text: str, echo_input: bool):
        if echo_input:
            await self._out.put(_resp(in_text=text))
        self._history.append({"role": "user", "content": text})
        await self._think()

    async def _think(self):
        async with self._busy:
            msgs = [{"role": "system", "content": self._system}] + self._history[-_MAX_HISTORY:]
            # never start the window on an orphaned tool message
            while len(msgs) > 1 and msgs[1].get("role") == "tool":
                msgs.pop(1)
            loop = asyncio.get_running_loop()
            try:
                msg = await loop.run_in_executor(None, self._chat, msgs)
            except Exception as e:
                self._log(f"[Pipeline] chat failed: {e}")
                await self._speak("Sorry, I couldn't reach any AI model just now.")
                return
            calls = msg.get("tool_calls") or []
            if calls:
                self._history.append({"role": "assistant", "content": msg.get("content") or None,
                                      "tool_calls": calls})
                fcs = []
                for c in calls:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    cid = c.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    self._pending_calls[cid] = c
                    fcs.append(SimpleNamespace(id=cid, name=fn.get("name"), args=args))
                await self._out.put(_resp(calls=fcs))
                return
            text = (msg.get("content") or "").strip()
            self._history.append({"role": "assistant", "content": text})
            await self._speak(text)

    def _chat(self, msgs) -> dict:
        last = None
        for prov, model in models.candidates("chat"):
            if models.cooling(prov, model):
                continue
            try:
                t0 = time.time()
                js = models.chat(prov, model, msgs, timeout=60,
                                 tools=self._tools or None,
                                 tool_choice="auto" if self._tools else None)
                self._log(f"[Pipeline] chat → {prov.get('preset', prov.get('id'))}/{model} "
                          f"({time.time() - t0:.1f}s)")
                return (js.get("choices") or [{}])[0].get("message") or {}
            except Exception as e:
                last = e
                self._log(f"[Pipeline] {prov.get('id')}/{model}: {str(e)[:140]}")
        raise RuntimeError(str(last) if last else "no chat model configured")

    async def _speak(self, text: str):
        text = (text or "").strip()
        if not text:
            await self._out.put(_resp(done=True))
            return
        clean = re.sub(r"[*_#`>]+", "", text)
        for sent in [s for s in _SENT.split(clean) if s.strip()]:
            await self._out.put(_resp(out_text=sent + " "))
            try:
                pcm = await self._tts(sent)
            except Exception as e:
                self._log(f"[Pipeline] TTS failed: {e}")
                pcm = b""
            for i in range(0, len(pcm), 9600):
                await self._out.put(_resp(data=pcm[i:i + 9600]))
        await self._out.put(_resp(done=True))

    async def _tts(self, text: str) -> bytes:
        import edge_tts
        import miniaudio
        mp3 = bytearray()
        async for chunk in edge_tts.Communicate(text, self._voice).stream():
            if chunk.get("type") == "audio":
                mp3.extend(chunk["data"])
        if not mp3:
            return b""
        dec = miniaudio.decode(bytes(mp3), output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=OUT_RATE)
        return dec.samples.tobytes()
