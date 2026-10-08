# Install Babata for a new owner

This guide is for a coding agent working for the person who will wear the glasses. Deploy the server first, verify it, then prepare that person's private AIUI project. The public source includes both the glasses client and the backend; it contains no existing person's data or access credentials.

Run server commands from the instance checkout's root unless a step explicitly changes directories. `/ABSOLUTE/PRIVATE/...` and `glasses.example.com` are examples to replace with this owner's paths and domain.

## 1. Collect the required inputs

Ask only for missing inputs; reuse facts the owner has already supplied:

| Input | What to establish |
| --- | --- |
| Deployment host | Owner-authorized SSH access to a Linux server, or a local Docker host for a server trial |
| Public HTTPS origin | A domain controlled by the owner, DNS, a trusted TLS certificate, and a route reachable from the phone/glasses network |
| Model credentials | The owner's provider and API key, available in a protected file or secure environment variable |
| Person ID | A stable nonsecret value such as `alice`, used consistently for this person's instance |
| Instance name | A unique Compose project name, such as `babata-alice` |
| Device access | The owner's Rokid account, phone with Hi Rokid, glasses version, and time to perform real-device checks |

The setup script generates the PostgreSQL password and the per-person gateway token. Do not ask the owner to paste a model key into a public issue or publish a private AIUI package. Use an existing secure credential channel. Never echo credentials or put them in command arguments.

The recipe is **one person per instance**. Two people may share a host, but use different checkout directories, project names, ports, tokens, database volumes, Codex state, and photo volumes. Merely signing into another Rokid account does not separate server memory.

## 2. Inspect the host before changing it

On the deployment host, inspect the current environment:

```sh
uname -sr
docker version
docker compose version
docker info --format '{{json .SecurityOptions}}'
df -h .
ss -ltn
```

Confirm Linux, Docker Engine, Compose, free disk space, and AppArmor support for the native Codex sandbox. The full deployment uses `deploy/security/babata-native` and `deploy/security/seccomp-native.json`. Docker Desktop can run local source tests; do not treat a local trial as a verified public server or weaken sandbox settings to bypass an unsupported host.

Inspect listeners on port 443 and the selected gateway port before deployment. Reuse an existing owner-controlled HTTPS reverse proxy where possible. Do not stop another service, replace its certificates, or take its port without the owner's authorization. Confirm DNS points to this host and public TCP 443 is permitted by the host firewall, network, and hosting provider. A local loopback response cannot establish public reachability.

## 3. Create fresh state and private configuration

Clone this public repository into a new instance directory:

```sh
git clone https://github.com/yunzhe99/babata.git babata-alice
cd babata-alice
mkdir -p /ABSOLUTE/PRIVATE/babata-alice
chmod 700 /ABSOLUTE/PRIVATE/babata-alice
```

Do not copy `.env`, volumes, auth files, conversation databases, memory files, photos, or Skills from any existing person's installation. The new instance starts empty. Do not reuse a previous Compose project name whose volumes may still exist.

Check the host policy with the supplied script:

```sh
python3 scripts/install_security.py --check
```

If the required Babata policy is absent and the owner has authorized host setup:

```sh
sudo python3 scripts/install_security.py --install
python3 scripts/install_security.py --check
```

Read that script's current help and output. If an existing policy differs, investigate before replacing anything; do not disable AppArmor, seccomp, or the private network to make the container start.

Have the owner place their model API key in a protected file such as `/ABSOLUTE/PRIVATE/babata-alice/model-api-key`. Give it mode `600`; do not display it. Then run the actual setup helper:

```sh
python3 scripts/setup_instance.py \
  --project babata-alice \
  --owner alice \
  --public-url https://glasses.example.com \
  --api-key-file /ABSOLUTE/PRIVATE/babata-alice/model-api-key \
  --client-config /ABSOLUTE/PRIVATE/babata-alice/rokid.json
```

The default provider is OpenAI. To use the supported DeepSeek path, add `--provider deepseek`; the script selects its supported model and memory settings. To use a different free loopback port, add `--gateway-port 8082`. The helper does not offer arbitrary model overrides; inspect the private settings and current supported provider code before changing them.

Setup writes a private `.env` in this instance's checkout and a private client JSON at the external path. It does not call the model and refuses to overwrite existing private configuration. Verify permissions and Git exclusions without printing the contents. Do not run a Compose command that prints interpolated environment values into logs.

The full glasses deployment uses `AGENT_RUNTIME=codex`. Its native-memory home is exclusively this person's. The generic SDK runtime can serve chat but is not the complete camera/native-memory installation described here. Desktop bridges, personal Skills, and external account connectors are optional integrations requiring separate setup.

## 4. Start the private server

```sh
docker compose up -d --build
docker compose ps
python3 scripts/check_health.py
```

The supplied health helper checks the private backend and unauthenticated gateway rejection with zero model calls. It cannot verify chat, memory, photographs, the native command sandbox, or public reachability.

The base configuration publishes only the Rokid gateway at `127.0.0.1:8081`. The assistant and PostgreSQL have no host ports. The `postgres_data`, `native_state`, and `photos` volumes are prefixed with the unique Compose project name. The gateway writes accepted normalized photos under `/photos` in its private photo volume.

If the gateway port is already in use, select a free port in the private instance configuration before starting. Keep the loopback binding. Give each person's reverse-proxy origin a route to their own gateway port.

Connect `https://glasses.example.com` to that gateway through the existing reverse proxy, preserving `/v1/chat`, `/v1/photo`, `/v1/device-result`, and `/v1/chat/stream`. Allow bounded photo bodies and sufficient request time for the configured client deadline. Forward only to the authenticated gateway. Do not expose the assistant's `/chat`, `/memory`, `/shared`, `/runtime`, database, or Codex app-server on the public origin.

There is also an optional `deploy/https/compose.yaml` overlay for a host without an existing proxy. Read that file before using it. In the private `.env`, set `HTTPS_PUBLISH_ADDRESS` to an explicitly available address and port, and set `TLS_CERTIFICATE_FILE` and `TLS_PRIVATE_KEY_FILE` to the domain's full-chain certificate and private key outside Git. Both files must be readable by container UID 10001. Do not change their permissions to expose them publicly.

If this host is approved to serve the domain directly on public 443, the address can be `0.0.0.0:443`. Then run:

```sh
docker compose -f compose.yaml -f deploy/https/compose.yaml up -d --build
python3 scripts/check_health.py --tls --gateway-url https://glasses.example.com
```

The overlay switches the gateway to TLS, adds the explicitly selected TLS publication, and also changes the existing loopback mapping to TLS. It does not remove that loopback mapping or decide that it owns port 443. Use the same two Compose files for subsequent operations on this TLS deployment. Public health validation checks the real domain certificate; the container's local TLS health probe does not.

Check logs only for relevant failures and keep their contents private. Do not include model keys, device tokens, messages, image bodies, or expanded configuration in reports. An `unhealthy` result needs investigation before preparing the glasses build.

## 5. Verify the server with live, nonsensitive data

Use a small client that loads the private `rokid.json`, supplies its token in the Authorization header, and does not print headers or configuration. Generate a fresh UUID for every new request. Keep request bodies and responses in a private temporary location. Do not embed the token into a shell command or browser URL.

Perform these checks in order. Record pass/fail and a brief observation, rather than publishing test content or personal data:

| Check | Required evidence |
| --- | --- |
| Public TLS and authentication | From the phone's external network, the domain's certificate validates; unauthenticated POST to `/v1/chat` is rejected with 401; the authorized request reaches the gateway |
| Native command sandbox | A model-requested scratch write/read works inside its workspace while the container and host isolation remain enforced; security preflight alone does not prove this |
| Real response | A nonsensitive request receives a model-generated nonempty answer, rather than only `/health` or HTTP 200 |
| Same-session context | Tell it a unique synthetic marker, then ask for that marker in a second turn with the same `session_id`; verify the returned value |
| Restart persistence | `docker compose restart babata gateway`, then ask for the marker with the same session; verify the value remains available. For the TLS overlay, include both Compose files |
| Explicit profile memory | Say “记住：我的测试标记是…” with `remember:true`; verify actual storage through the private server, then query it from a new session after restart |
| Automatic memory | Send ordinary turns with `remember:false`, inspect the backend's memory status/files after its actual idle/maintenance conditions, and query the resulting memory from a new session; do not infer this from same-session recall |
| Server camera decision | Send a clear current-scene request with `capabilities.camera:true`; verify a bound, expiring `take_photo` action, then return one synthetic or nonsensitive JPEG/PNG to `/v1/device-result` with its original session, request UUID, and action UUID |
| Photo follow-up | Check a real model image answer and at least one follow-up in the same session; verify a negative camera request does not initiate capture |
| Photo files | Inspect the private `/photos` volume: a nonempty, decodable JPEG, matching sidecar and `manifest.jsonl` entry must exist; an answer alone does not establish this |
| Two-person isolation | Set up a second fresh project with a different token and `--gateway-port 8082`; store different synthetic markers using identical client session IDs. Each instance must recall only its own marker. Inspect that its database, native memory, and photo volumes differ |
| Token separation | Sending person A's token to person B's public origin must return 401, and conversely |

Photo action receipts are single-use and expire. Reusing a receipt should fail, rather than taking or accepting a second photo. A server restart discards an in-flight action; issue a fresh request after restart. Never claim that failed camera work produced a picture.

The photo archive is best effort. Disk-full or permission failures can leave the model answer working while file storage fails. Check the file and manifest; verify storage ownership and available space if missing. Also check Docker startup and the declared restart policies before claiming the service will return after a host reboot; test a reboot only in an owner-approved maintenance window. New deployment acceptance has not been performed by publishing this repository, and must be recorded by the installer.

## 6. Prepare the private glasses project

Use Node.js 22.15+ on a machine with the public checkout and its own protected copy of the generated client JSON. If the server is remote, transfer only this person's `rokid.json` through their authorized secure channel. Keep it outside the local checkout.

```sh
node mobile/rokid-aiui/scripts/prepare.mjs \
  --config /ABSOLUTE/PRIVATE/babata-alice/rokid.json \
  --out /ABSOLUTE/PRIVATE/babata-alice/rokid-build
```

The parent output directory must already exist; `rokid-build` must not exist. The helper copies the AIUI source and inserts this person's full HTTPS `/v1/chat` endpoint and gateway token. The resulting project and any AIX package are private credentials-bearing artifacts.

Run the client checks from the public source:

```sh
cd mobile/rokid-aiui
npm test
cd ../..
```

For international glasses, open [AIUI Global](https://aiui-global.rokid.com/) and use the owner's Rokid account. Import the prepared private folder. GitHub import can select `mobile/rokid-aiui`, but that public project has placeholders; supply the private configuration only inside the owner's private Studio project before packaging, and never commit it back to GitHub.

Package AIX through the Studio build controls and save the project information. In the Hi Rokid phone app, use the same account and the paired glasses, then update their resources. Follow the current [official quickstart](https://js.rokid.com/AIUI/guide/quickstart/quickstart?lang=en-US&version=latest) and its [official source](https://github.com/yodaos-project/AIUI/blob/main/documentation/0-guide/quickstart/quickstart.en-US.md) for exact controls. Domestic users can use [AIUI Studio](https://aiui.rokid.com/) with the corresponding phone app.

This is a private debugging build. Do not submit the token-bearing project to the public agent store. A store-ready product would need a separate user authentication design.

## 7. Perform real glasses acceptance

The owner must wear and operate the glasses. Record device/app versions and check each result:

1. Open Babata using the configured wake phrase and confirm its foreground page appears.
2. Speak two consecutive Chinese turns without tapping between them. The microphone must remain stopped during playback and reopen after it ends.
3. Ask to see the current scene. Confirm exactly one real photo, an answer matching what is in front of the wearer, a same-session follow-up, and the new private archive file.
4. Ask an ordinary question about photos without asking for a current capture; verify no camera activation.
5. Save a nonsensitive marker explicitly. Exit and reopen, restart the server, then verify recall. Check automatic background memory separately after its processing conditions are met.
6. Try wake interruption during playback, then say “退出” or switch away. Verify listening and playback stop, and late responses/photos do not restart the old conversation.
7. Deny camera permission or temporarily lose the network. Verify the user sees a failure and the assistant does not claim it saw a missing image.

Studio preview, local tests, packaging, and successful HTTPS calls cannot stand in for these observations. New international-device compatibility, automatic background memory, playback interruption, and exit cleanup remain unverified until each check passes on that installation.

## 8. Hand over a concise result

Provide the owner with their public endpoint, private configuration/build locations, and how to update or stop their instance. Do not include secrets or test contents. State which server checks and glasses checks passed, and list any remaining checks plainly.

Use `docker compose stop` to pause the instance, and `docker compose start` to resume it. For direct TLS, always include `-f compose.yaml -f deploy/https/compose.yaml` in Compose commands. Never use `docker compose down -v` for routine updates; it deletes the persistent data. Back up each person's database, native state, and photos separately through their approved private storage.

For updates, pull the chosen source revision, rerun relevant local checks, rebuild, then repeat the checks affected by the change. Keep the project name, owner, private `.env`, and volumes consistent. A leaked gateway token requires rotation on the server and a new private glasses build.
