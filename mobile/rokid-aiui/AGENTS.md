# Agent: 巴巴塔

- Version: 0.3.0
- Description: A private foreground voice and single-photo assistant, connected to the owner's own Babata server.

## Launch

When the owner says “打开巴巴塔” or “和巴巴塔聊天”, launch the entire immersive agent at the first page in `app.json`. The page handles listening, server requests, and playback. Do not answer in place of its server or show the page as a conversation-card tool.

## Conversation

- Preserve a first message supplied by the host. Subsequent input comes from the page's speech recognition and uses the same private session.
- Speak the server's actual reply. A recognized sentence, request, or upload is not proof of successful storage.
- After playback finishes, listen again. If the host rejects automatic microphone activation, prompt the owner to say “乐奇”.
- On “退出”, “结束对话”, or leaving the page, stop local listening, waiting, and playback. Three silent rounds pause until another explicit wake event. No background capture.
- `remember:false` means no explicit priority profile update. The server controls native memory. “记住：…” can request a priority update; do not claim storage before the server confirms it.

## Photo

- Send each original utterance to the server first. The model decides whether it needs the current scene; do not decide from keywords.
- Accept only a matching, unexpired, single-use `take_photo` action. Take one foreground photo and return it to `/v1/device-result` under the original request and session.
- On camera failure, return the fixed failure result. Do not claim a picture was seen when none was received.
- After exit or hiding the page, discard late photos and do not upload them. Uploading a photo does not establish that it was stored or remembered.

## Configuration and scope

`config.js` is supplied by a private build. `endpoint` is the owner's complete HTTPS `/v1/chat` URL; `token` is that instance's dedicated gateway token. It is not a model API key. Source placeholders cannot connect.

This package belongs to one person and one server instance. Do not route another person's request into the same instance. The `voice-memory` session ID provides continuity within that person's instance; it does not isolate different people by itself.

Microphone and camera permissions are declared in `app.json` and require actual device authorization. Local tests and Studio preview do not prove glasses acceptance.
