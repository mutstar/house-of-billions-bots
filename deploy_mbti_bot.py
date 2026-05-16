"""
MBTI 봇 GCP VM 배포 스크립트
- GCS에 파일 업로드
- VM에 SSH 접속 후 배포
"""
import json
import subprocess
import sys
import os
from pathlib import Path

# ── 설정 ──────────────────────────────────────────────────────
KEY_FILE     = r"C:\Users\071staff\Downloads\my-stock-bot-488502-804216e8fef9.json"
PROJECT_ID   = "my-stock-bot-488502"
BUCKET_NAME  = "billions-mbti-bot-files"
VM_NAME      = "billions-meetup-bot"
VM_ZONE      = "us-central1-a"
VM_IP        = "34.121.75.220"
IMAGE_DIR    = Path(r"C:\Users\071staff\Downloads\MBTI")
BOT_FILE     = Path(r"C:\Users\071staff\Desktop\dokdo-evaluation-main\dokdo-evaluation-main\billions_mbti_bot.py")
REMOTE_DIR   = "/home/mbti_bot"

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = KEY_FILE

from google.cloud import storage
from google.oauth2 import service_account
import google.auth
import google.auth.transport.requests
import requests as req
import paramiko
import cryptography


def upload_to_gcs():
    print("[1/2] GCS 버킷에 파일 업로드 중...")
    client = storage.Client.from_service_account_json(KEY_FILE)

    # 버킷 생성 (이미 있으면 그냥 get)
    try:
        bucket = client.create_bucket(BUCKET_NAME, location="us-central1")
        print(f"  버킷 생성: {BUCKET_NAME}")
    except Exception:
        bucket = client.bucket(BUCKET_NAME)
        print(f"  기존 버킷 사용: {BUCKET_NAME}")

    # 봇 파일 업로드
    blob = bucket.blob("billions_mbti_bot.py")
    blob.upload_from_filename(str(BOT_FILE))
    print(f"  OK{BOT_FILE.name}")

    # 이미지 업로드
    for img in IMAGE_DIR.glob("*.png"):
        blob = bucket.blob(f"images/{img.name}")
        blob.upload_from_filename(str(img))
        print(f"  OK{img.name}")

    print("GCS 업로드 완료!\n")


def get_oslogin_username(credentials):
    """서비스 계정의 OS Login 유저명 조회"""
    authed = google.auth.transport.requests.Request()
    credentials.refresh(authed)
    token = credentials.token

    with open(KEY_FILE) as f:
        key_data = json.load(f)
    email = key_data["client_email"]
    user_id = email.replace("@", "_").replace(".", "_")
    return f"sa_{key_data['client_id']}"


def deploy_via_ssh():
    print("[2/2] SSH 배포 중...")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    import io

    # SSH 키 생성
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    pub_key_str = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH
    ).decode()
    priv_key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption()
    )

    # Compute Engine API로 인스턴스 메타데이터에 SSH 키 추가
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    token = creds.token
    username = "mbtibot"

    # 현재 인스턴스 메타데이터 조회
    get_url = f"https://compute.googleapis.com/compute/v1/projects/{PROJECT_ID}/zones/{VM_ZONE}/instances/{VM_NAME}"
    inst = req.get(get_url, headers={"Authorization": f"Bearer {token}"}).json()
    meta = inst.get("metadata", {})
    items = meta.get("items", [])

    # ssh-keys 항목 업데이트
    ssh_key_value = f"{username}:{pub_key_str}"
    new_items = [i for i in items if i.get("key") != "ssh-keys"]
    new_items.append({"key": "ssh-keys", "value": ssh_key_value})
    meta["items"] = new_items

    set_url = f"{get_url}/setMetadata"
    resp = req.post(
        set_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=meta
    )
    resp.raise_for_status()
    print(f"  SSH 키 등록 완료 (user: {username})")

    import time
    time.sleep(5)  # 메타데이터 적용 대기

    # paramiko로 접속
    pkey = paramiko.RSAKey.from_private_key(io.StringIO(priv_key_pem.decode()))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(VM_IP, username=username, pkey=pkey, timeout=30)
    print("  SSH 접속 성공!")

    sftp = ssh.open_sftp()

    # 원격 디렉토리 생성
    stdin, stdout, stderr = ssh.exec_command(f"mkdir -p {REMOTE_DIR}/images")
    stdout.channel.recv_exit_status()

    # 봇 파일 전송
    sftp.put(str(BOT_FILE), f"{REMOTE_DIR}/billions_mbti_bot.py")
    print(f"  OKbillions_mbti_bot.py 전송")

    # 이미지 전송
    for img in IMAGE_DIR.glob("*.png"):
        sftp.put(str(img), f"{REMOTE_DIR}/images/{img.name}")
        print(f"  OK{img.name} 전송")

    sftp.close()

    # 패키지 설치 + 봇 실행 (기존 프로세스 종료 후 재시작)
    commands = [
        "pip3 install python-telegram-bot -q",
        f"pkill -f billions_mbti_bot.py || true",
        f"export MBTI_IMAGE_DIR={REMOTE_DIR}/images && nohup python3 {REMOTE_DIR}/billions_mbti_bot.py > {REMOTE_DIR}/bot.log 2>&1 &",
        "echo '봇 시작됨'"
    ]
    for cmd in commands:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if out:
            print(f"  {out}")
        if err and exit_code != 0:
            print(f"  WARN{err}")

    ssh.close()
    print("\n[DONE] 배포 완료!")
    print(f"로그 확인: {REMOTE_DIR}/bot.log")


if __name__ == "__main__":
    try:
        upload_to_gcs()
        deploy_via_ssh()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
