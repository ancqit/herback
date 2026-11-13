# herback

FastAPI storage for [herbant](https://github.com/ancqit/herbant). Files go to a Cloudflare R2 bucket through the S3 API, the same way junctionBack does in `app/r2_media.py`: the API signs a short PUT, the browser uploads straight to the bucket, and reads use the public CDN URL.

Render boots this with the Dockerfile. `GET /health` is the health check.

## MongoDB

The client matches junctionBack `app/database.py`: `MongoClient` on `MONGODB_URL`, pool 5–100, `retryWrites=True`. The database name is `MONGODB_DATABASE`. It defaults to `herb`, so the same Atlas cluster can keep Junction's `junction` database next to this one.

Put this in `.env` and in Render. Paste your cluster URL over the host. Replace the password. If the password contains `@`, `:`, `/`, `#`, or `?`, encode those characters (`%40`, `%3A`, `%2F`, `%23`, `%3F`) or the host will be read wrong.

```bash
MONGODB_URL=mongodb+srv://YOUR_USERNAME:YOUR_PASSWORD@YOUR_CLUSTER.mongodb.net/?retryWrites=true&w=majority&appName=herback
MONGODB_DATABASE=herb
```

Atlas side, once:

1. Database Access: a user that can read and write the `herb` database. That username and password go in the URL.
2. Network Access: allow this service to reach the cluster. A Render web service has no fixed outbound address, so the entry that works is `0.0.0.0/0`.
3. Connect → Drivers → Python, and copy the `mongodb+srv` string into `MONGODB_URL`.

`GET /health` stays up even when the URL is empty, and it reports `mongo_configured` plus `mongo_host` (the cluster host only). `GET /mongo` runs a ping and returns `{"ok":true,"database":"herb","host":"..."}`. A failed ping is 503 and the body does not include the URL.

## Room snapshots

herbant keeps one picture in memory so moving through the room stays quick. `POST /api/room` stores that picture in `room_snapshots`. `GET /api/room?room=herbalife` returns the newest one. `GET /api/schedules?room=herbalife` reads the schedule days out of that same picture, so a dump does not drop a schedule.

The browser deletes its local copy about 30 seconds after a dump succeeds. Mongo keeps the newest 24 dumps for the room and deletes the older ones.

## Scrap

herback is the holder for the scrap portal. The portal calls `/api/scrap/...` and Vercel strips `/api`, so these routes are mounted at `/scrap` and `/api/scrap`.

| Route | What it does |
| --- | --- |
| `POST /scrap/login` | `2001` returns `{ role: "slave" }`. `UPLOAD_TOKEN` returns `{ role: "admin" }`. |
| `POST /scrap/get-upload-url` | Admin. Presigned PUT under `2001/`. |
| `GET /scrap/scraps` | Card list from `2001/catalog.json`. |
| `POST /scrap/scraps` | Admin. Saves a card into that catalog. |
| `DELETE /scrap/scraps/{id}` | Admin. Removes the card and its objects. |
| `GET /scrap/types` | `bikes` plus types already stored. |

Set `UPLOAD_TOKEN` next to the `R2_*` variables. Scrap keys cannot land in `herb/` or `shorts/`.

## Binter

herback is the holder for the Sunskriti metals-and-glass collect portal. The portal calls `/api/binter/...` and Vercel strips `/api`, so these routes are mounted at `/binter` and `/api/binter`, the same way as scrap.

Three numbers are set on this service, beside `HERB_API_KEY` and `UPLOAD_TOKEN`. The operator fills them on Render. A blank value keeps that door shut. There is no default password.

| Number | Role |
| --- | --- |
| `BINTER_NOTIFIER_TOKEN` | The person who raises a collect. |
| `BINTER_RECEIVER_TOKEN` | The vehicle that accepts and completes it. |
| `BINTER_ADMIN_TOKEN` | The weekly and monthly sheet. |

Send the number as `Authorization: Bearer <token>`. The receiver also sends `X-Binter-Vehicle` on the profile, the accept, the complete, and the receiver's list.

The three buckets are ferrous metal (steel, iron, and other magnetic scrap), non-ferrous metal (aluminium, copper, brass, and other metal that is not magnetic), and glass (bottles, jars, and broken glass). Location types are indoor, yard, and gate. There is no map. Each collect gets a number like `C1842`.

The notifier sends a place label, a bucket, a photograph, and an optional clip. The receiver registers a name, a vehicle number, and three papers: aadhaar, driving licence, and vehicle RC. After save, the profile says only whether each paper is on file and its filename. It does not return the document object key or a URL. Trash photos and clips do return a public URL so the vehicle can see the bucket.

Accepting a collect stores the receiver's name and vehicle number. That is when the notifier's Received lamp lights. Only that vehicle can mark the collect done. A second accept is refused. The receiver's list shows open collects plus ones that vehicle accepted which are not done. The notifier and the admin still see done collects.

Thirty days after `created_at`, herback deletes the photo and clip objects and drops the request. Profile documents stay. The sweep runs on every read and write of `binter/state.json`.

Keys stay under `binter/`. The server chooses them. Nothing lands in `herb/`, `2001/`, or `shorts/`.

| Object | Key |
| --- | --- |
| State | `binter/state.json` |
| Photograph | `binter/photos/<32 hex>.jpg`, `.png`, or `.webp` (8 MB) |
| Clip | `binter/clips/<32 hex>.mp4`, `.webm`, or `.mov` (20 MB, `max_seconds` 10) |
| Papers | `binter/documents/<aadhaar|licence|rc>/<32 hex>.jpg`, `.png`, `.webp`, or `.pdf` (8 MB) |

| Route | What it does |
| --- | --- |
| `POST /binter/login` | Body `{ token }`. Returns `{ ok, role }` for notifier, receiver, or admin. |
| `GET /binter/meta` | Program name, the three buckets, and the location types. |
| `POST /binter/upload-url` | Presigned PUT. Photographs and clips for the notifier. Papers for the receiver. |
| `GET /binter/profile` | Receiver, with `X-Binter-Vehicle`. |
| `POST /binter/profile` | Receiver saves the name, vehicle, and papers. |
| `GET /binter/requests` | Notifier and admin see every unexpired collect. The receiver sees open collects and the ones that vehicle accepted which are not done. |
| `POST /binter/requests` | Notifier raises a collect. |
| `POST /binter/requests/{id}/accept` | Receiver with papers on file. |
| `POST /binter/requests/{id}/complete` | The accepting vehicle only. |
| `GET /binter/gist` | Admin counts for the collects still inside 30 days. |
| `GET /binter/sheet?span=week` | Admin rows whose `created_at` is in the current week. Week starts Monday 00:00, Asia/Kolkata. |
| `GET /binter/sheet?span=month` | Admin rows for the current month. The month starts on the 1st at 00:00, Asia/Kolkata. |

The R2 bucket CORS policy must allow the binter Vercel origin to PUT from the browser, the same way it allows herbant. Include `https://binter.vercel.app` and `http://localhost:5173`. Without that, `POST /binter/upload-url` succeeds and the browser PUT fails.

## Cloudflare R2

1. In the Cloudflare dashboard open **R2 → Create bucket**. Name it `herb-media` (or another name you will paste into `R2_BUCKET`). Pick a location close to the centre if Cloudflare asks. This name is not the public hostname.
2. Open the bucket → **Settings → Public access**. Turn on a public read URL. Prefer a custom domain (for example `media.yourdomain`) over the `r2.dev` URL. Copy that origin with no path and no trailing slash into `R2_PUBLIC_BASE_URL`. Public access is read-only. Writes stay on the API token.
3. Add a **CORS policy** on the bucket so herbant can PUT from the browser. Without this, `POST /media/sign` succeeds and the browser PUT fails.

```json
[
  {
    "AllowedOrigins": [
      "https://herbant.vercel.app",
      "https://binter.vercel.app",
      "http://localhost:4200",
      "http://localhost:5173"
    ],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["Content-Type"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

Add each Vercel preview origin you actually use, or allow `https://herbant-*.vercel.app` if the dashboard accepts that pattern. The PUT must send the `Content-Type` this API returns. That header is part of the signature.

4. Optional cache rule, same idea as Junction shorts: on the public hostname, cache `herb/*` with **Cache Everything**. Object keys are random, so a long cache is safe.
5. **R2 → Overview** shows the account id. That value is `R2_ACCOUNT_ID`. The S3 endpoint is `https://<account id>.r2.cloudflarestorage.com`. Leave `R2_ENDPOINT` empty unless you were given a different endpoint. The jurisdiction endpoint (`https://<account id>.eu.r2.cloudflarestorage.com` and similar) must be set explicitly when the bucket is not in the default location.
6. **R2 → Manage API tokens → Create API token**.
   - Permission: **Object Read & Write**
   - Specify bucket: only `herb-media`
   - Do not create an Admin Read & Write token for this service
   - Copy **Access Key ID** into `R2_ACCESS_KEY_ID` and **Secret Access Key** into `R2_SECRET_ACCESS_KEY`. The secret is shown once. If you lose it, create a new token and replace both values. Revoke the old token after the new one works.

The API talks to R2 as S3. You do not create an AWS account, and you do not set `AWS_ACCESS_KEY_ID`. The boto3 client uses the R2 endpoint with `region_name=auto` and signature version `s3v4`.

## Render

1. Push this repo, then in Render choose **New → Blueprint** and point it at `render.yaml`. The service name is `herback`, region Singapore, Docker runtime, health check `/health`. Pick Free if Render offers it, otherwise the smallest web service. The blueprint does not pin a plan.
2. Render asks for every `sync: false` key. Paste the Cloudflare values. Generate the app secret locally and paste it as `HERB_API_KEY`:

```bash
openssl rand -hex 32
```

3. `R2_ENDPOINT` and `CORS_ORIGINS` can stay empty. An empty `R2_ENDPOINT` uses `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`. `CORS_ORIGINS` only adds origins on top of the built-in list: `http://localhost:4200`, `http://127.0.0.1:4200`, `http://localhost:5173`, `http://127.0.0.1:5173`, `https://herbant.vercel.app`, `https://scrap-sooty.vercel.app`, and `https://binter.vercel.app`.
4. Deploy. Open `https://<your-service>.onrender.com/health`. You want:

```json
{"status":"ok","service":"herback","r2_configured":true,"r2_missing":[],"api_key_configured":true,"binter_notifier_configured":true,"binter_receiver_configured":true,"binter_admin_configured":true,"mongo_configured":true,"mongo_missing":[],"mongo_host":"YOUR_CLUSTER.mongodb.net"}
```

`r2_missing` lists variable **names** only. A missing name means that slot is empty in Render. Edit the env var and redeploy if the start command already captured the old environment. Render injects env at boot.

5. After it is live, set `OPENAPI_ENABLED` to `false` if you do not want `/docs` public. `/health` stays open. Upload routes stay behind `HERB_API_KEY`.

## What not to commit

| Name | Where it lives | Notes |
| --- | --- | --- |
| `R2_SECRET_ACCESS_KEY` | Render env, local `.env` | Shown once by Cloudflare |
| `R2_ACCESS_KEY_ID` | same | Pair it with the secret. Not an AWS key |
| `R2_ACCOUNT_ID` | same | Not secret, but keep it with the other R2 values |
| `R2_BUCKET` | same | Bucket name |
| `R2_PUBLIC_BASE_URL` | same | Safe to be public. herbant will need it to render images |
| `HERB_API_KEY` | Render env | Optional. `1001` can sign herbant media. This key still works on `X-Herb-Key` or `Authorization: Bearer` |
| `UPLOAD_TOKEN` | Render env, local `.env` | Scrap desk login. `2001` stays the read-only login |
| `BINTER_NOTIFIER_TOKEN` | Render env, local `.env` | Number the authority gives the person who raises a collect |
| `BINTER_RECEIVER_TOKEN` | Render env, local `.env` | Number the authority gives the vehicle |
| `BINTER_ADMIN_TOKEN` | Render env, local `.env` | Number for the weekly and monthly sheet |
| `MONGODB_URL` | Render env, local `.env` | Contains the database username and password |
| `MONGODB_DATABASE` | same | `herb` unless you choose another name on this cluster |
| `.env` | your machine only | `.gitignore` already excludes it |

`HERB_API_KEY` stops strangers from minting upload URLs. If the Angular app later ships that key in the browser bundle, anyone who loads the site can read it. That is a gate for a private room, not a user login. When herbant calls this API, prefer keeping the key on a server you control, or replace this check with a real session. Do not put the R2 secret in herbant. The browser only ever sees a presigned URL that expires (default 10 minutes) and can write one object key.

## Call the API

Presign, then PUT the bytes to `upload_url` with the returned `Content-Type`:

```bash
curl -s https://<your-service>.onrender.com/media/sign \
  -H "X-Herb-Key: $HERB_API_KEY" \
  -H "content-type: application/json" \
  -d '{"kind":"image","content_type":"image/jpeg","filename":"shelf.jpg"}'
```

`kind` is `image` (jpeg, png, webp, gif, 8 MB), `document` (pdf, 15 MB), `audio` (mp3, m4a, aac, wav, webm, 20 MB), or `video`. Video follows junctionBack shorts and jMonster: mp4, webm, and mov, 40 MB, signed PUT with that `Content-Type`. A phone `.m4v` or `application/mp4` is stored as mp4. Keys look like `herb/videos/schedule/<32 hex>.mp4`. `place` is `schedule`, `coach`, or `centre`. The same bucket holds scrap under `2001/` and these files under `herb/`, never under `shorts/`.

The herbant admin pass `1001` can mint the URL (`Authorization: Bearer 1001` or `X-Herb-Key`). `HERB_API_KEY` still works for a caller outside the room. The user pass `2001` cannot sign. `public_url` is the lasting read URL.

`POST /media` accepts a multipart file (`kind` + `file`) and uploads through this server. Use that only when bucket CORS is not ready yet. The presigned PUT is the path to keep.

`DELETE /media` with `{"object_key":"herb/images/..."}` and the same API key removes the object. Keys outside the `herb/` prefixes are rejected.

## Local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

`GET http://127.0.0.1:8000/health`, `GET http://127.0.0.1:8000/mongo`, and `GET http://127.0.0.1:8000/docs`.
