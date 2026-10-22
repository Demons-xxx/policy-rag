"""PolicyRAG 一键启动脚本。

常用：
    python start.py            启动服务并打开网页（已在运行则直接打开）
    python start.py status     查看状态
    python start.py stop       停止服务
    python start.py restart    重启
    python start.py install    首次安装（建虚拟环境、装依赖、构建前端）

Windows 上直接双击 start.bat 也可以。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / 'data' / 'state' / 'server.pid'
LOG_FILE = ROOT / 'log' / 'server.log'
ENV_FILE = ROOT / '.env'
ENV_EXAMPLE = ROOT / '.env.example'
WEB_DIST = ROOT / 'web' / 'dist'
MODEL_DIR = ROOT / 'models' / 'bge-base-zh-v1.5'
RELEASE_FILE = ROOT / 'data' / 'state' / 'active_release.json'
MIRROR = 'https://pypi.tuna.tsinghua.edu.cn/simple'


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def say(message: str = '') -> None:
    try:
        print(message, flush=True)
    except (BrokenPipeError, OSError):
        # 输出被 head 之类的管道提前关闭时，把 stdout 指向空设备，避免退出时报错
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass


# 输出被重定向到管道/文件时统一用 UTF-8，避免中文乱码；直接双击运行时沿用控制台编码。
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def venv_python() -> Path:
    candidate = ROOT / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    return candidate if candidate.is_file() else Path(sys.executable)


def has_venv() -> bool:
    venv = ROOT / '.venv'
    candidate = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return venv.is_dir() and candidate.is_file()


def port_open(port: int, timeout: float = 1.0) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def http_json(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        return None


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding='ascii').strip())
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == 'nt':
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                                 capture_output=True, text=True, timeout=10).stdout or ''
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def config_value(key: str, default: str = '') -> str:
    """从环境变量或 .env 里读一个配置项。"""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            name, value = line.split('=', 1)
            if name.strip().lstrip('\ufeff') == key:
                return value.strip()
    except Exception:
        pass
    return default


def base_port() -> int:
    return int(config_value('POLICYRAG_PORT', '7880') or 7880)


def open_page(url: str) -> bool:
    """用系统默认浏览器打开页面，失败时返回 False（调用方提示手动打开）。"""
    if os.name == 'nt':
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:
            pass
        try:
            subprocess.Popen(['cmd', '/c', 'start', '', url], shell=False)
            return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def base_url() -> str:
    return f'http://127.0.0.1:{base_port()}'


# ---------------------------------------------------------------------------
# 首次安装
# ---------------------------------------------------------------------------
def do_install() -> int:
    say('== PolicyRAG 首次安装 ==')
    python = venv_python()
    if not (ROOT / '.venv').is_dir():
        say('1/4 创建虚拟环境 .venv ...')
        if subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')]).returncode != 0:
            say('创建虚拟环境失败。请先安装 Python 3.10 及以上版本。')
            return 1
        python = venv_python()
    else:
        say('1/4 虚拟环境已存在，跳过')

    say('2/4 安装 Python 依赖（使用清华镜像）...')
    code = subprocess.run([str(python), '-m', 'pip', 'install', '-r',
                           str(ROOT / 'requirements.txt'), '-i', MIRROR]).returncode
    if code != 0:
        say('依赖安装失败，可重试：.venv\\Scripts\\python.exe -m pip install -r requirements.txt -i ' + MIRROR)
        return code

    if not ENV_FILE.is_file() and ENV_EXAMPLE.is_file():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        say('3/4 已生成 .env（请填写模型 API 后再启动）')
    else:
        say('3/4 .env 已存在，跳过')

    if not MODEL_DIR.is_dir():
        say('    缺少向量模型：' + str(MODEL_DIR))
        say('    请向维护者索取 bge-base-zh-v1.5 文件夹，或执行：')
        say('      python -c "from huggingface_hub import snapshot_download; snapshot_download(\'BAAI/bge-base-zh-v1.5\', local_dir=\'models/bge-base-zh-v1.5\')"')
    else:
        say('    向量模型已就绪')

    if not WEB_DIST.is_dir() and shutil.which('npm'):
        say('4/4 构建前端 ...')
        npm = 'npm.cmd' if os.name == 'nt' else 'npm'
        subprocess.run([npm, 'install', '--registry=https://registry.npmmirror.com'], cwd=ROOT / 'web')
        subprocess.run([npm, 'run', 'build'], cwd=ROOT / 'web')
    else:
        say('4/4 前端已构建（或未安装 Node，跳过）')

    say('\n安装完成。接下来运行：python start.py')
    return 0


# ---------------------------------------------------------------------------
# 启动 / 停止 / 状态
# ---------------------------------------------------------------------------
def ensure_env() -> bool:
    if ENV_FILE.is_file():
        return True
    if ENV_EXAMPLE.is_file():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        say('已根据 .env.example 生成 .env。')
    say('请先编辑 ' + str(ENV_FILE) + '，填写 POLICYRAG_LLM_BASE_URL / API_KEY / MODEL。')
    return False


def ensure_ready() -> bool:
    ok = True
    if not has_venv():
        say('未找到虚拟环境 .venv，请先运行：python start.py install')
        ok = False
    if not MODEL_DIR.is_dir():
        say('缺少向量模型目录：' + str(MODEL_DIR) + '（见 README 第 4 节）')
        ok = False
    if not WEB_DIST.is_dir():
        say('前端尚未构建：web/dist 不存在，页面会显示占位内容（cd web && npm install && npm run build）')
    return ok


def start_server(open_browser: bool = False) -> int:
    port = base_port()
    if port_open(port):
        info = http_json(base_url() + '/api/info')
        if info:
            say(f'服务已经在运行：{base_url()}')
            print_info(info)
            if open_browser and not open_page(base_url() + '/'):
                say('浏览器未能自动打开，请手动访问：' + base_url() + '/')
            else:
                say('请手动打开：' + base_url() + '/')
            return 0
        say(f'端口 {port} 被其他程序占用，请修改 .env 里的 POLICYRAG_PORT 或结束占用进程。')
        return 1

    if not ensure_env() or not ensure_ready():
        return 1

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open('ab')
    command = [str(venv_python()), '-m', 'uvicorn', 'app.main:app',
               '--host', '127.0.0.1', '--port', str(port), '--log-level', 'info']
    creationflags = 0
    kwargs = {}
    if os.name == 'nt':
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs['start_new_session'] = True
    try:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=creationflags, **kwargs)
    except Exception as exc:
        say(f'启动失败：{exc}')
        return 1
    PID_FILE.write_text(str(process.pid), encoding='ascii')
    say(f'正在启动服务（日志 {LOG_FILE}）...')

    deadline = time.time() + 120
    while time.time() < deadline:
        if process.poll() is not None:
            say('服务进程已退出，最后 20 行日志：')
            say('\n'.join(LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()[-20:]))
            return 1
        info = http_json(base_url() + '/api/info')
        if info:
            say(f'\n服务已就绪：{base_url()}')
            print_info(info)
            if open_browser:
                if open_page(base_url() + '/'):
                    say('已打开浏览器。')
                else:
                    say('浏览器未能自动打开，请手动访问：' + base_url() + '/')
            else:
                say('请手动打开：' + base_url() + '/（加 --open 可自动打开浏览器）')
            return 0
        time.sleep(1)
    say('等待服务就绪超时，请查看日志：' + str(LOG_FILE))
    return 1


def print_info(info: dict) -> None:
    say(f"  版本：{info.get('version')}    知识库：{info.get('release_id') or '尚未发布'}")
    say(f"  文档：{info.get('doc_count')} 份    单元：{info.get('unit_count')} 个    "
        f"向量库：{info.get('vector_backend')}")
    ready = '正常' if info.get('model_ready') else ('不可用：' + str(info.get('error') or '未知原因'))
    say(f"  生成模型：{info.get('llm_model')}（{ready}）")


def do_status() -> int:
    port = base_port()
    info = http_json(base_url() + '/api/info')
    if info:
        say(f'运行中：{base_url()}')
        print_info(info)
        pid = read_pid()
        if pid and pid_alive(pid):
            say(f'  进程号：{pid}')
        return 0
    if port_open(port):
        say(f'端口 {port} 有程序在监听，但 /api/info 无响应，可能不是 PolicyRAG。')
        return 1
    say('未运行。启动命令：python start.py')
    return 0


def do_stop() -> int:
    pid = read_pid()
    if pid and pid_alive(pid):
        say(f'正在停止进程 {pid} ...')
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, text=True)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(20):
            if not port_open(base_port()):
                break
            time.sleep(0.5)
    elif port_open(base_port()):
        say(f'服务在运行，但不是本脚本启动的（没有 pid 文件），请在它的窗口按 Ctrl+C 停止。')
        return 1
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    say('已停止。')
    return 0


def do_restart(open_browser: bool) -> int:
    do_stop()
    time.sleep(1)
    return start_server(open_browser)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description='PolicyRAG 一键启动')
    parser.add_argument('command', nargs='?', default='start',
                        choices=['start', 'status', 'stop', 'restart', 'install'],
                        help='start（默认）/ status / stop / restart / install')
    parser.add_argument('--open', '--browser', dest='open_browser', action='store_true',
                        help='启动后自动打开系统浏览器（默认不打开，用 DSH 侧边栏访问即可）')
    parser.add_argument('--no-open', dest='open_browser', action='store_false',
                        help='兼容旧参数，等价于默认行为')
    parser.set_defaults(open_browser=False)
    args = parser.parse_args()
    open_browser = bool(args.open_browser)

    if args.command == 'install':
        return do_install()
    if args.command == 'status':
        return do_status()
    if args.command == 'stop':
        return do_stop()
    if args.command == 'restart':
        return do_restart(open_browser)
    return start_server(open_browser)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say('\n已取消。')
        sys.exit(130)
