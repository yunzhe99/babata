# Babata · Rokid AIUI client

This AIUI 0.18.x project opens a foreground voice session. It listens to one sentence, sends the original text to the owner's server, speaks the reply, and listens again after actual playback ends. Chinese wake, memory, and exit phrases are built in.

[Full agent installation guide](../../docs/AGENT_INSTALL.md)

## Behavior

- The server model decides whether to answer or request one photo. The client never decides from camera keywords.
- Each camera action is bound to the current request and session, expires, and can be executed once. The result goes to the same origin at `/v1/device-result`.
- The microphone is stopped during playback. The next listening round starts after `audioPlayer.onEnded` plus a short quiet interval.
- A new wake event, hiding the page, or saying “退出” or “结束对话” clears the current local work. Late replies and late photos are discarded. Three silent rounds pause listening.
- Photos are taken only in the foreground. The client does not create a video stream, capture in the background, or save images to its logs.

These are implementation behaviors. New devices must check continuous speech, microphone permissions, camera permissions, interruption, and exit cleanup on real glasses. Cancelling local work cannot promise cancellation of a server request or a camera operation already started.

## Private configuration

The public `config.js` has placeholders. Use a dedicated gateway token, never a model API key. Save configuration outside the checkout:

```json
{
  "endpoint": "https://YOUR_DOMAIN/v1/chat",
  "token": "YOUR_DEDICATED_GATEWAY_TOKEN",
  "sessionId": "voice-memory",
  "timeoutMs": 120000
}
```

The full server setup generates this JSON at the private path you choose. Both the configuration and the generated project must remain outside the checkout. The parent output directory must exist, and the output project must not already exist:

```sh
node mobile/rokid-aiui/scripts/prepare.mjs \
  --config /ABSOLUTE/PRIVATE/rokid.json \
  --out /ABSOLUTE/PRIVATE/rokid-build
```

The prepared `config.js`, uploaded Studio project, and generated AIX package contain the token. Use them only with the owner's account. Do not publish them to an app store, upload them to Git, or share the package. Rotate the gateway token if one is exposed.

## Import and sync

1. Open [AIUI Global](https://aiui-global.rokid.com/) for international glasses, or [AIUI Studio](https://aiui.rokid.com/) for the domestic workflow. Use the owner's Rokid account.
2. Import the prepared private project based on `mobile/rokid-aiui`, not the checkout with placeholder configuration. Check microphone and camera permissions in the project details.
3. Package and save it to that account's cloud resources. Follow the [official international quickstart](https://js.rokid.com/AIUI/guide/quickstart/quickstart?lang=en-US&version=latest) for the current controls.
4. Use the same account in the Hi Rokid phone app, connect the glasses, and update glasses resources through the available developer/AIUI controls. Domestic app labels can differ.
5. Say “乐奇，打开巴巴塔”. Verify two spoken turns without tapping, a real photo answer, an exit, and recall after reopening.

Updating an existing Studio project should retain its Agent ID and private configuration. Importing another local project may create a new project; do not assume it replaces the previous one.

## Request contract

The configured endpoint is the full HTTPS `/v1/chat` URL. Requests use `Authorization: Bearer <gateway token>` and do not provide `user_id`; the gateway binds its token to the person.

```json
{
  "message": "看看眼前这个是什么",
  "session_id": "voice-memory",
  "request_id": "a fresh UUID for this request",
  "remember": false,
  "capabilities": {"camera": true}
}
```

A normal response has a nonempty `reply`. A photo decision returns an empty `reply` and an `action` with `type: "take_photo"`, its UUID, and an expiry. The client checks the binding and returns either one JPEG/PNG or a fixed camera error code to `/v1/device-result`.

`remember:false` means no explicit priority profile update, rather than disabling server native memory. “记住：…” sets `remember:true`; querying what was remembered remains an ordinary turn. Memory formation and photo storage are server responsibilities. The client displays the actual reply and cannot establish that a background memory job finished.

Network failure pauses listening and keeps a short diagnostic on screen. There is no automatic retry, because a timed-out request may already have been processed. Reopen or explicitly wake after resolving the cause.

## Local checks

Use Node.js 22.15+; no JavaScript dependencies are required:

```sh
cd mobile/rokid-aiui
npm test
```

Tests mock the AIUI `wx` module and run without isolated workers for compatibility with newer Node releases.

With the official AIX CLI already installed, keep package output outside the checkout:

```sh
aix pack /ABSOLUTE/PRIVATE/rokid-build --engine '^0.18.0' \
  -o /ABSOLUTE/PRIVATE/babata.aix
aix list /ABSOLUTE/PRIVATE/babata.aix
aix show /ABSOLUTE/PRIVATE/babata.aix
```

Tests and package inspection do not validate public HTTPS, real audio, camera authorization, or international-device compatibility. Use the full guide's acceptance checklist.
