# Reve API Notes

## Exploration Status
- [x] JS bundle analysis (reverse-engineered from `reveapi-BSwKUoG7.js`)
- [ ] Manual browser verification (recommended to confirm)

## Findings

### Base URL
```
https://app.reve.com
```

### Authentication
- **Method**: Bearer token in `Authorization` header
- **Token format**: JWT (Base64-encoded)
- **Token storage key**: `reve:bearer_token` (localStorage)
- **User info storage**: `reve:user_info` (localStorage)
- **Header**: `Authorization: Bearer <token>`
- **Optional**: `Proxy-Authorization: Bearer <iap_token>` (IAP proxy auth)

### API Endpoints (from JS bundle)

#### Auth & User
- `POST /api/misc/login` — email/password login → returns `bearer_token`
- `GET /api/misc/userinfo` — get current user info (returns `user.default_project`)
- `POST /api/misc/signup_finalize` — signup
- `DELETE /api/misc/login` — logout

#### Generation (Image)
- `POST /api/misc/chat` — **main generation endpoint** (chat-based)
  - Headers: `Content-Type: application/json; charset=utf-8`, `Cache-Control: max-age=0, no-cache`
  - Body: JSON (chat messages array + project context)
- `POST /api/proto/model_infer_sync` — direct model inference
  - Body: `{ model_id, project_id, inputs, origin: "rnd" }`
- `GET /api/project/{project_id}/generation/{id}` — get generation status/result

#### Images
- `GET /api/project/{project_id}/image/{id}` — get image metadata
- `GET /api/project/{project_id}/image/{id}/url` — get image blob URL
- URL pattern: `api/project/{project_id}/image/{id}/url/filename/{id}`

#### Projects
- `GET /api/project/{project_id}/layer_configuration` — get layer configs
- `POST /api/project/{project_id}/filter` — create filter/effect

### Image URL Format
```
https://app.reve.com/api/project/{project_id}/image/{image_id}/url/filename/{image_id}
```

### How Generation Works
1. User types prompt → `POST /api/misc/chat` with messages array
2. Server processes → returns generation tasks
3. Frontend polls `GET /api/project/{project_id}/generation/{id}` for status
4. When complete, image available via `GET /api/project/{project_id}/image/{id}/url`

### Key localStorage Keys
- `reve:bearer_token` — auth token
- `reve:user_info` — user info JSON
- `reve:current_project` — active project ID
- `reve:readonly` — readonly mode flag

## Known Issue
- The exact JSON body format for `POST /api/misc/chat` could not be fully extracted from minified JS.
- Recommendation: Use Playwright to intercept the actual chat POST request while generating an image manually.
- Alternative: The `/api/proto/model_infer_sync` endpoint has a clearer format: `{ model_id, project_id, inputs, origin: "rnd" }`
