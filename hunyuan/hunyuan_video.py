#!/usr/bin/env python3
"""
Генерация видео через aistudio.tencent.com (HunyuanVideo 1.5).
Использует сохранённую сессию из hunyuan_session.json.

Запуск:
  python3 hunyuan/hunyuan_video.py "dark city rain night"
  python3 hunyuan/hunyuan_video.py "rain falling" --image path/to/image.jpg
  python3 hunyuan/hunyuan_video.py "rain falling" --image path/to/image.jpg --ratio 16:9 --out outputs/video.mp4
"""
import sys
import json
import time
import argparse
import requests
from pathlib import Path

SESSION_FILE = Path(__file__).parent / "hunyuan_session.json"
API_BASE = "https://api.hunyuan.tencent.com"
SITE_BASE = "https://aistudio.tencent.com"

# Model configs
T2V_APP_ID = 302
T2V_MODEL_ID = 10645
T2V_MODEL_NAME = "hunyuan-video-1.5-t2v"
T2V_MODEL = "hunyuan-video-1.5-t2v-720p-v1.0.1"
T2V_GEN_PATH = "/openapi/v1/videos/generations/submission"
T2V_QUERY_PATH = "/openapi/v1/videos/generations/task"

I2V_APP_ID = 303
I2V_MODEL_ID = 10646
I2V_MODEL_NAME = "hunyuan-video-1.5-i2v"
I2V_MODEL = "hunyuan-video-1.5-i2v-720p-v1.0.1"
I2V_GEN_PATH = "/openapi/v1/videos/generations/submission"
I2V_QUERY_PATH = "/openapi/v1/videos/generations/task"

RATIO_MAP = {
    "16:9": "1280*720",
    "9:16": "720*1280",
    "1:1":  "720*720",
    "smart": "smart",
}


def load_session() -> dict:
    if not SESSION_FILE.exists():
        print("❌ Сессия не найдена. Сначала запусти: python3 hunyuan/hunyuan_auth.py")
        sys.exit(1)
    state = json.loads(SESSION_FILE.read_text())
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])
               if "tencent.com" in c.get("domain", "")}
    if not cookies:
        print("❌ Куки пустые. Повтори авторизацию.")
        sys.exit(1)
    return cookies


def make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Referer": f"{SITE_BASE}/",
        "Origin": SITE_BASE,
        "Content-Type": "application/json",
        "X-Source": "web",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    })
    return s


def get_cid(s: requests.Session) -> str:
    r = s.post(f"{API_BASE}/api/vision_platform/generate/cid", json={})
    r.raise_for_status()
    data = r.json()
    cid = data.get("cid") or data.get("data", {}).get("cid")
    if not cid:
        print("❌ Не удалось получить CID:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return cid


def upload_image(s: requests.Session, image_path: Path) -> str:
    """Загружает изображение в Tencent COS через SDK, возвращает resourceId."""
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        print("❌ Установи: pip install cos-python-sdk-v5")
        sys.exit(1)

    r = s.post(f"{API_BASE}/api/vision_platform/resource/genUploadInfo",
               json={"resourceType": "image", "fileName": image_path.name})
    try:
        info = r.json()
    except Exception:
        info = None
    # ответ может быть вложен в data (как cid) — развернуть
    if isinstance(info, dict) and info.get("resourceId") is None and isinstance(info.get("data"), dict):
        info = info["data"]
    if not isinstance(info, dict):
        print(f"❌ genUploadInfo пустой/невалидный ответ: HTTP {r.status_code}, body: {r.text[:500]}")
        sys.exit(1)

    resource_id = info.get("resourceId")
    location = info.get("location")
    if not location:
        print("❌ location не найден:", json.dumps(info, ensure_ascii=False)[:300])
        sys.exit(1)

    cos = CosS3Client(CosConfig(
        Region="ap-guangzhou",
        SecretId=info["encryptTmpSecretId"],
        SecretKey=info["encryptTmpSecretKey"],
        Token=info["encryptToken"],
        Timeout=120,
    ))
    suffix = image_path.suffix.lstrip(".").lower() or "jpg"
    cos.put_object(
        Bucket="hy-model-ap-prod-1258344703",
        Body=image_path.read_bytes(),
        Key=location,
        ContentType=f"image/{suffix}",
    )
    print(f"  COS upload OK")
    return resource_id


def get_signed_url(s: requests.Session, resource_id: str) -> str:
    """Получает подписанный URL для загруженного ресурса."""
    r = s.get(f"{API_BASE}/api/vision_platform/resource/download",
              params={"resourceId": resource_id, "resourceType": "image"})
    r.raise_for_status()
    data = r.json()
    url = data.get("realUrl") or data.get("url") or data.get("data", {}).get("realUrl")
    if not url:
        print("❌ realUrl не найден:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return url


def start_t2v(s: requests.Session, cid: str, prompt: str, resolution: str) -> str:
    payload = {
        "cid": cid,
        "modelId": T2V_MODEL_ID,
        "appId": T2V_APP_ID,
        "modelPath": T2V_GEN_PATH,
        "modelName": T2V_MODEL_NAME,
        "model": T2V_MODEL,
        "queryPath": T2V_QUERY_PATH,
        "revise": True,
        "duration": 5,
        "n": 1,
        "prompt": prompt,
        "resolution": resolution,
    }
    r = s.post(f"{API_BASE}/api/vision_platform/generation", json=payload)
    r.raise_for_status()
    try:
        data = r.json()
    except Exception:
        data = None
    print(f"  gen resp: HTTP {r.status_code} "
          f"{json.dumps(data, ensure_ascii=False)[:400] if data is not None else r.text[:400]}")
    if not isinstance(data, dict):
        print("❌ generation: пустой/невалидный ответ сервера")
        sys.exit(1)
    task_id = (data.get("taskId") or data.get("data", {}).get("taskId")
               or data.get("JobsDetail", {}).get("JobId"))
    if not task_id:
        print("❌ taskId не найден")
        sys.exit(1)
    return task_id


def start_i2v(s: requests.Session, cid: str, image_url: str, prompt: str, resolution: str) -> str:
    payload = {
        "cid": cid,
        "modelId": I2V_MODEL_ID,
        "appId": I2V_APP_ID,
        "modelPath": I2V_GEN_PATH,
        "modelName": I2V_MODEL_NAME,
        "model": I2V_MODEL,
        "queryPath": I2V_QUERY_PATH,
        "revise": True,
        "duration": 5,
        "n": 1,
        "image_url": image_url,
        "prompt": prompt,
    }
    r = s.post(f"{API_BASE}/api/vision_platform/generation", json=payload)
    r.raise_for_status()
    try:
        data = r.json()
    except Exception:
        data = None
    print(f"  gen resp: HTTP {r.status_code} "
          f"{json.dumps(data, ensure_ascii=False)[:400] if data is not None else r.text[:400]}")
    if not isinstance(data, dict):
        print("❌ generation: пустой/невалидный ответ сервера")
        sys.exit(1)
    task_id = (data.get("taskId") or data.get("data", {}).get("taskId")
               or data.get("JobsDetail", {}).get("JobId"))
    if not task_id:
        print("❌ taskId не найден")
        sys.exit(1)
    return task_id


def poll_task(s: requests.Session, task_id: str, query_path: str, timeout: int = 1800) -> str:
    print(f"⏳ Генерация... (до {timeout}с)", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = s.post(f"{API_BASE}/api/vision_platform/query_task",
                   json={"taskId": task_id})  # queryPath не нужен
        r.raise_for_status()
        data = r.json()

        status = str(data.get("status", ""))
        if data.get("type") == "finish" or status in ("succeeded", "SUCCESS", "DONE", "success", "done", "5"):
            print(" ✅")
            result_str = data.get("result", "")
            if result_str:
                try:
                    result = json.loads(result_str)
                    videos = result.get("videos", [])
                    if videos:
                        return videos[0].get("url", "")
                except Exception:
                    pass
            return ""
        elif status in ("failed", "FAIL", "FAILED", "fail", "-1", "4"):
            print(" ❌")
            print("Ошибка:", data.get("message", ""), json.dumps(data, ensure_ascii=False)[:300])
            sys.exit(1)
        print(".", end="", flush=True)
        time.sleep(10)
    print(" ⏰ timeout")
    sys.exit(1)


def download(url: str, out_path: Path):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    print(f"💾 Сохранено → {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="Текстовый промпт для видео")
    parser.add_argument("--image", default="", help="Путь к изображению (i2v режим)")
    parser.add_argument("--ratio", default="16:9", choices=list(RATIO_MAP.keys()))
    parser.add_argument("--out", default="", help="Путь для сохранения видео")
    args = parser.parse_args()

    resolution = RATIO_MAP[args.ratio]
    ts = int(time.time())
    mode = "i2v" if args.image else "t2v"
    out_path = Path(args.out) if args.out else Path("hunyuan/outputs") / f"video_{mode}_{ts}.mp4"

    print(f"🎬 Режим: {mode}")
    print(f"📝 Промпт: {args.prompt}")
    print(f"📐 Разрешение: {resolution}")

    cookies = load_session()
    s = make_session(cookies)

    print("🔑 Получаю CID...")
    cid = get_cid(s)
    print(f"   cid: {cid}")

    if mode == "i2v":
        image_path = Path(args.image)
        if not image_path.exists():
            print(f"❌ Файл не найден: {image_path}")
            sys.exit(1)
        print("📤 Загружаю изображение в COS...")
        resource_id = upload_image(s, image_path)
        print(f"   resourceId: {resource_id}")
        print("🔗 Получаю подписанный URL...")
        image_url = get_signed_url(s, resource_id)
        print(f"   imageUrl: {image_url[:80]}...")
        print("🚀 Запускаю i2v генерацию...")
        task_id = start_i2v(s, cid, image_url, args.prompt, resolution)
        query_path = I2V_QUERY_PATH
    else:
        print("🚀 Запускаю t2v генерацию...")
        task_id = start_t2v(s, cid, args.prompt, resolution)
        query_path = T2V_QUERY_PATH

    print(f"   taskId: {task_id}")
    video_url = poll_task(s, task_id, query_path)

    if not video_url:
        print("❌ URL видео не получен. Проверь формат ответа.")
        sys.exit(1)

    print(f"🔗 URL: {video_url[:80]}...")
    download(video_url, out_path)


if __name__ == "__main__":
    main()
