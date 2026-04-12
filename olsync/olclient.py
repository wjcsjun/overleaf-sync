"""Overleaf Client"""
import requests as reqs
from bs4 import BeautifulSoup
import json
import uuid
import socket
import ssl
import base64
import os
import struct
import time

LOGIN_URL = "https://www.overleaf.com/login"
PROJECT_URL = "https://www.overleaf.com/project"
DOWNLOAD_URL = "https://www.overleaf.com/project/{}/download/zip"
UPLOAD_URL = "https://www.overleaf.com/project/{}/upload"
FOLDER_URL = "https://www.overleaf.com/project/{}/folder"
DELETE_URL = "https://www.overleaf.com/project/{}/doc/{}"
COMPILE_URL = "https://www.overleaf.com/project/{}/compile?enable_pdf_caching=true"
BASE_URL = "https://www.overleaf.com"
PATH_SEP = "/"


def _ws_send(sock, text):
    data = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes([data[i] ^ mask[i % 4] for i in range(len(data))])
    length = len(data)
    if length < 126:
        header = bytes([0x81, 0x80 | length]) + mask
    else:
        header = bytes([0x81, 0xFE, length >> 8, length & 0xFF]) + mask
    sock.sendall(header + masked)


def _ws_recv(sock):
    def recv_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            buf += chunk
        return buf

    header = recv_exact(2)
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(8))[0]
    payload = recv_exact(length)
    if opcode == 8:
        return None
    if opcode == 9:
        return "__ping__"
    return payload.decode("utf-8", errors="replace")


def _open_ws(host, path, cookies_str, timeout=16):
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Origin: https://{host}\r\n"
        f"Cookie: {cookies_str}\r\n"
        f"Cache-Control: no-cache\r\n"
        f"Pragma: no-cache\r\n"
        f"\r\n"
    )
    ctx = ssl.create_default_context()
    raw_sock = socket.create_connection((host, 443), timeout=timeout)
    tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
    tls_sock.settimeout(timeout)
    tls_sock.sendall(handshake.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += tls_sock.recv(4096)
    status_line = buf.split(b"\r\n")[0].decode()
    if "101" not in status_line:
        raise ConnectionError(f"WebSocket handshake failed: {status_line}")
    return tls_sock


class OverleafClient(object):
    @staticmethod
    def filter_projects(json_content, more_attrs=None):
        more_attrs = more_attrs or {}
        for p in json_content:
            if not p.get("archived") and not p.get("trashed"):
                if all(p.get(k) == v for k, v in more_attrs.items()):
                    yield p

    def __init__(self, cookie=None, csrf=None):
        self._cookie = cookie
        self._csrf = csrf

    def _get_cookies_str(self):
        if isinstance(self._cookie, dict):
            return "; ".join(f"{k}={v}" for k, v in self._cookie.items())
        # CookieJar
        return "; ".join(f"{c.name}={c.value}" for c in self._cookie)

    def _open_socket(self, project_id):
        """Open a raw WebSocket to Overleaf Socket.IO and return (tls_sock, session_id)."""
        host = "www.overleaf.com"
        # Build a requests.Session to carry cookies and pick up GCLB
        session = reqs.Session()
        if isinstance(self._cookie, dict):
            for name, value in self._cookie.items():
                session.cookies.set_cookie(
                    reqs.cookies.create_cookie(name, value, domain=".overleaf.com")
                )
        else:
            session.cookies = self._cookie

        time_now = int(time.time() * 1000)
        r = session.get(
            f"https://{host}/socket.io/1/?projectId={project_id}&t={time_now}",
            timeout=16
        )
        r.raise_for_status()
        socket_id = r.text.split(":")[0]

        cookies_str = "; ".join([
            f"{c.name}={c.value}"
            for c in session.cookies
            if c.domain.endswith(".overleaf.com")
        ])

        ws_path = f"/socket.io/1/websocket/{socket_id}?projectId={project_id}"
        tls_sock = _open_ws(host, ws_path, cookies_str)
        return tls_sock

    def login(self, username, password):
        get_login = reqs.get(LOGIN_URL)
        self._csrf = BeautifulSoup(get_login.content, 'html.parser').find(
            'input', {'name': '_csrf'}).get('value')
        login_json = {"_csrf": self._csrf, "email": username, "password": password}
        post_login = reqs.post(LOGIN_URL, json=login_json, cookies=get_login.cookies)
        if post_login.status_code == 200 and get_login.cookies["overleaf_session2"] != post_login.cookies["overleaf_session2"]:
            self._cookie = post_login.cookies
            self._cookie['GCLB'] = get_login.cookies['GCLB']
            projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
            self._csrf = BeautifulSoup(projects_page.content, 'html.parser').find(
                'meta', {'name': 'ol-csrfToken'}).get('content')
            return {"cookie": self._cookie, "csrf": self._csrf}
    ''''
    def all_projects(self):
        projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
        json_content = json.loads(
            BeautifulSoup(projects_page.content, 'html.parser').find(
                'meta', {'name': 'ol-projects'}).get('content'))
        return list(OverleafClient.filter_projects(json_content))

    def get_project(self, project_name):
        projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
        json_content = json.loads(
            BeautifulSoup(projects_page.content, 'html.parser').find(
                'meta', {'name': 'ol-projects'}).get('content'))
        return next(OverleafClient.filter_projects(json_content, {"name": project_name}), None)
    '''
    def all_projects(self):
        projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
        soup = BeautifulSoup(projects_page.content, 'html.parser')
        meta = soup.find('meta', {'name': 'ol-prefetchedProjectsBlob'})
        if meta is None:
            # fallback 旧版
            meta = soup.find('meta', {'name': 'ol-projects'})
            json_content = json.loads(meta.get('content'))
        else:
            json_content = json.loads(meta.get('content'))["projects"]
        return list(OverleafClient.filter_projects(json_content))

    def get_project(self, project_name):
        projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
        soup = BeautifulSoup(projects_page.content, 'html.parser')
        meta = soup.find('meta', {'name': 'ol-prefetchedProjectsBlob'})
        if meta is None:
            meta = soup.find('meta', {'name': 'ol-projects'})
            json_content = json.loads(meta.get('content'))
        else:
            json_content = json.loads(meta.get('content'))["projects"]
        return next(OverleafClient.filter_projects(json_content, {"name": project_name}), None)
    

    def download_project(self, project_id):
        r = reqs.get(DOWNLOAD_URL.format(project_id), stream=True, cookies=self._cookie)
        return r.content

    def create_folder(self, project_id, parent_folder_id, folder_name):
        params = {"parent_folder_id": parent_folder_id, "name": folder_name}
        headers = {"X-Csrf-Token": self._csrf}
        r = reqs.post(FOLDER_URL.format(project_id), cookies=self._cookie,
                      headers=headers, json=params)
        if r.ok:
            return json.loads(r.content)
        elif r.status_code == str(400):
            return
        else:
            raise reqs.HTTPError()

    def get_project_infos(self, project_id):
        project_infos = None
        tls_sock = self._open_socket(project_id)
        try:
            while True:
                msg = _ws_recv(tls_sock)
                if msg is None:
                    break
                if msg == "__ping__":
                    tls_sock.sendall(bytes([0x8A, 0x00]))
                    continue
                if msg == "1::":
                    payload = json.dumps({"name": "joinProject",
                                          "args": [{"project_id": project_id}]})
                    _ws_send(tls_sock, f"5:1+::{payload}")
                elif msg.startswith("2::"):
                    _ws_send(tls_sock, "2::")
                elif msg.startswith("5:") and "joinProjectResponse" in msg:
                    data = json.loads(msg[len("5:"):].lstrip(":"))
                    project_infos = data["args"][0]["project"]
                    break
                elif msg.startswith("6:::1+"):
                    args = json.loads(msg[6:])
                    if len(args) > 1 and args[1]:
                        project_infos = args[1]
                    break
        finally:
            tls_sock.close()
        return project_infos
    
    def upload_file(self, project_id, project_infos, file_name, file_size, file):
        folder_id = project_infos['rootFolder'][0]['_id']
        if PATH_SEP in file_name:
            local_folders = file_name.split(PATH_SEP)[:-1]
            current_overleaf_folder = project_infos['rootFolder'][0]['folders']
            for local_folder in local_folders:
                exists_on_remote = False
                for remote_folder in current_overleaf_folder:
                    if local_folder.lower() == remote_folder['name'].lower():
                        exists_on_remote = True
                        folder_id = remote_folder['_id']
                        current_overleaf_folder = remote_folder['folders']
                        break
                if not exists_on_remote:
                    new_folder = self.create_folder(project_id, folder_id, local_folder)
                    current_overleaf_folder.append(new_folder)
                    folder_id = new_folder['_id']
                    current_overleaf_folder = new_folder['folders']

        mime = "application/octet-stream"
        base_name = file_name.split(PATH_SEP)[-1]
        r = reqs.post(
            f"https://www.overleaf.com/project/{project_id}/upload?folder_id={folder_id}",
            cookies=self._cookie,
            files={
                "relativePath": (None, "null"),
                "name": (None, base_name),
                "type": (None, mime),
                "qqfile": (base_name, file, mime),
            },
            headers={
                "x-csrf-token": self._csrf,
                "Referer": f"https://www.overleaf.com/project/{project_id}",
                "Accept": "application/json",
            }
        )
        return r.status_code == 200 and json.loads(r.content).get("success", False)

    

    def delete_file(self, project_id, project_infos, file_name):
        file = None
        if PATH_SEP in file_name:
            local_folders = file_name.split(PATH_SEP)[:-1]
            current_overleaf_folder = project_infos['rootFolder'][0]['folders']
            for local_folder in local_folders:
                for remote_folder in current_overleaf_folder:
                    if local_folder.lower() == remote_folder['name'].lower():
                        file = next(
                            (v for v in remote_folder['docs']
                             if v['name'] == file_name.split(PATH_SEP)[-1]), None)
                        current_overleaf_folder = remote_folder['folders']
                        break
        else:
            file = next(
                (v for v in project_infos['rootFolder'][0]['docs']
                 if v['name'] == file_name), None)
        if file is None:
            return False
        headers = {"X-Csrf-Token": self._csrf}
        r = reqs.delete(DELETE_URL.format(project_id, file['_id']),
                        cookies=self._cookie, headers=headers, json={})
        return r.status_code == str(204)

    def download_pdf(self, project_id):
        headers = {
            "X-Csrf-Token": self._csrf,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        body = {
            "check": "silent",
            "draft": False,
            "incrementalCompilesEnabled": True,
            "rootDoc_id": "",
            "stopOnFirstError": False,
        }
        r = reqs.post(COMPILE_URL.format(project_id), cookies=self._cookie,
                      headers=headers, json=body)
        if not r.ok:
            raise reqs.HTTPError()
        compile_result = r.json()
        if compile_result.get("status") != "success":
            raise reqs.HTTPError()

        output_files = compile_result.get("outputFiles", [])
        # 优先取 output.pdf（主编译产物）
        pdf_file = next(
            (f for f in output_files if f["type"] == "pdf" and f["url"].endswith("output.pdf")),
            None,
        )
        # 退而求其次取列表中最后一个 PDF
        if pdf_file is None:
            pdf_files = [f for f in output_files if f["type"] == "pdf"]
            pdf_file = pdf_files[-1] if pdf_files else None

        if pdf_file is None:
            return None, None

        pdf_url = BASE_URL + pdf_file["url"]
        r = reqs.get(pdf_url, cookies=self._cookie, headers=headers, stream=True)

        # Overleaf 有时将编译产物托管在 compiles.overleafusercontent.com
        if not r.ok or r.headers.get("content-type", "") != "application/pdf":
            pdf_url = "https://compiles.overleafusercontent.com" + pdf_file["url"]
            r = reqs.get(pdf_url, cookies=self._cookie, headers=headers, stream=True)

        if not r.ok:
            return None, None

        content = b"".join(chunk for chunk in r.iter_content(chunk_size=8192) if chunk)
        file_name = pdf_file.get("path", "output.pdf")
        return file_name, content