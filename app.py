from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import json
import os
import subprocess
import random
import string
import uuid
from datetime import datetime, timedelta
import sys
import shutil
import threading
import time
import zipfile
import psutil

app = Flask(__name__)
app.secret_key = 'velatrix-super-secret-key-2026'
# Upload limit increased to 500MB
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

USERS_FILE = 'users.json'
BOTS_DIR = 'bots'
CPU_HISTORY = {}
CRASH_COUNT = {}
NET_STATS = {}

os.makedirs(BOTS_DIR, exist_ok=True)

# ============================================
# Rate Limit
# ============================================

class RateLimiter:
    def check_rate(self, server_id, limit_percent):
        if server_id not in CPU_HISTORY:
            CPU_HISTORY[server_id] = []
        users = load_users()
        server = None
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    server = s
                    break
        if not server or server.get('status') != 'running':
            return False, 0
        pid = server.get('pid')
        if not pid: return False, 0
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=1)
            now = time.time()
            CPU_HISTORY[server_id].append({'time': now, 'cpu': cpu})
            CPU_HISTORY[server_id] = [h for h in CPU_HISTORY[server_id] if now - h['time'] < 30]
            recent = [h['cpu'] for h in CPU_HISTORY[server_id] if now - h['time'] < 10]
            if recent:
                avg_cpu = sum(recent) / len(recent)
                if avg_cpu > limit_percent:
                    return True, avg_cpu
        except: pass
        return False, 0

rate_limiter = RateLimiter()

# ============================================
# Auto-Restart
# ============================================

def should_auto_restart(server_id):
    if server_id not in CRASH_COUNT:
        CRASH_COUNT[server_id] = {'count': 0, 'last_crash': time.time()}
    crash_info = CRASH_COUNT[server_id]
    if time.time() - crash_info['last_crash'] < 60:
        if crash_info['count'] >= 3:
            return False
    else:
        crash_info['count'] = 0
    crash_info['count'] += 1
    crash_info['last_crash'] = time.time()
    return True

# ============================================
# Helpers
# ============================================

def generate_random_password(length=10):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

def load_users():
    if not os.path.exists(USERS_FILE):
        default = {"admin": {"password": "velatrix567", "role": "velatrix"}}
        save_users(default)
        return default
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'admin' not in data:
        data['admin'] = {"password": "velatrix567", "role": "velatrix"}
        save_users(data)
    return data

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_server_dir(server_id):
    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)
    return server_dir

def check_server_valid(server_id):
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                expiry = s.get('expiry', '')
                if expiry:
                    try:
                        exp_date = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S.%f')
                        if datetime.now() > exp_date:
                            return False, "expired"
                    except: pass
                return True, s
    return False, "deleted"

def get_server_by_id(server_id):
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                return s, uname
    return None, None

def create_default_files(server_dir, server_type='python'):
    stype = server_type.lower()
    if 'node' in stype:
        main_file = os.path.join(server_dir, 'index.js')
        if not os.path.exists(main_file):
            with open(main_file, 'w', encoding='utf-8') as f:
                f.write('console.log("Node.js Server is running on VELATRIX HOSTING!");\nsetInterval(() => console.log("Heartbeat active..."), 10000);')
        pkg_file = os.path.join(server_dir, 'package.json')
        if not os.path.exists(pkg_file):
            with open(pkg_file, 'w', encoding='utf-8') as f:
                f.write('{"name": "app", "version": "1.0.0", "main": "index.js", "dependencies": {}}')
    elif 'static' in stype:
        main_file = os.path.join(server_dir, 'index.html')
        if not os.path.exists(main_file):
            with open(main_file, 'w', encoding='utf-8') as f:
                f.write('<!DOCTYPE html>\n<html>\n<head>\n<title>Static Website</title>\n<style>body{background:#0a0c12;color:white;text-align:center;padding:50px;font-family:sans-serif;}</style>\n</head>\n<body>\n<h1>Website is Live on Velatrix Hosting!</h1>\n</body>\n</html>')
    else:
        main_py = os.path.join(server_dir, 'main.py')
        if not os.path.exists(main_py):
            with open(main_py, 'w', encoding='utf-8') as f:
                f.write('# VELATRIX HOSTING - Default Bot\nimport time\nprint("Bot is running on VELATRIX HOSTING")\nwhile True:\n    print("Heartbeat active")\n    time.sleep(10)\n')
        req_file = os.path.join(server_dir, 'requirements.txt')
        if not os.path.exists(req_file):
            with open(req_file, 'w', encoding='utf-8') as f:
                f.write('# Add your pip packages here\n')

# ============================================
# Bot Run System
# ============================================

def run_bot(server_id, main_file='main.py', requirements_file='requirements.txt'):
    server_dir = get_server_dir(server_id)
    log_file = os.path.join(server_dir, 'output.log')
    python_exe = sys.executable
    
    def log(msg):
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
                f.flush()
        except: pass
    
    if os.path.exists(log_file):
        try: os.remove(log_file)
        except: open(log_file, 'w').close()
    
    ts = lambda: datetime.now().strftime('%I:%M:%S %p')
    
    server, _ = get_server_by_id(server_id)
    server_type = server.get('type', 'python').lower() if server else 'python'
    cpu_limit = server.get('cpu_limit', 80) if server else 80
    
    log(f"[{ts()}] Starting Server ID: {server_id}")
    log(f"[{ts()}] Server Type: {server_type.upper()}")
    log(f"[{ts()}] Rate limit: {cpu_limit}%")
    log("")
    
    cmd_list = []
    
    if 'node' in server_type:
        if not main_file or not main_file.endswith('.js'): main_file = 'index.js'
        main_path = os.path.join(server_dir, main_file)
        if not os.path.exists(main_path): return None, f"ERROR: {main_file} not found!"
        
        pkg_path = os.path.join(server_dir, 'package.json')
        if os.path.exists(pkg_path):
            log(f"[{ts()}] Running: npm install")
            try:
                subprocess.run(['npm', 'install'], cwd=server_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                log(f"[{ts()}] Packages installed successfully.")
            except Exception as e:
                log(f"[{ts()}] npm install skipped or failed: {str(e)}")
        log(f"[{ts()}] Running: node {main_file}")
        cmd_list = ['node', os.path.abspath(main_path)]
        
    elif 'static' in server_type:
        log(f"[{ts()}] Starting Static Web Server...")
        log(f"[{ts()}] Serving HTML files...")
        cmd_list = [python_exe, '-m', 'http.server', '0']
        
    else:
        if not main_file: main_file = 'main.py'
        main_path = os.path.join(server_dir, main_file)
        if not os.path.exists(main_path): return None, f"ERROR: {main_file} not found!"
        
        if requirements_file and requirements_file.strip():
            req_path = os.path.join(server_dir, requirements_file.strip())
            if os.path.exists(req_path):
                log(f"[{ts()}] Running: pip install -r {requirements_file}")
                try:
                    subprocess.run([python_exe, '-m', 'pip', 'install', '-r', os.path.abspath(req_path), '--disable-pip-version-check'], cwd=server_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    log(f"[{ts()}] Requirements check complete.")
                except: pass
        log(f"[{ts()}] Running: python {main_file}")
        cmd_list = [python_exe, os.path.abspath(main_path)]
    
    log("")
    
    try:
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        
        proc = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=server_dir,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1, env=env, universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        
        log(f"[{ts()}] Server marked as running successfully")
        log(f"[{ts()}] PID: {proc.pid}")
        log("")
        
        def rate_monitor():
            while proc.poll() is None:
                time.sleep(5)
                exceeded, avg_cpu = rate_limiter.check_rate(server_id, cpu_limit)
                if exceeded:
                    log(f"[{datetime.now().strftime('%I:%M:%S %p')}] CPU Limit! {avg_cpu:.1f}% > {cpu_limit}%")
                    proc.terminate()
                    time.sleep(2)
                    if proc.poll() is None: proc.kill()
                    
                    users = load_users()
                    for uname, data in users.items():
                        if uname == 'admin': continue
                        servers = data.get('servers', [])
                        if not isinstance(servers, list): continue
                        for s in servers:
                            if isinstance(s, dict) and s.get('server_id') == server_id:
                                s['status'] = 'stopped'
                                s['pid'] = None
                                s['rate_limit_exceeded'] = True
                                s['stopped_by_user'] = False
                                save_users(users)
                                break
                    break
        
        threading.Thread(target=rate_monitor, daemon=True).start()
        
        def stream_output():
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    for line in iter(proc.stdout.readline, ''):
                        if line:
                            line = line.rstrip('\n\r')
                            if line:
                                f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {line}\n")
                                f.flush()
            except: pass
        
        threading.Thread(target=stream_output, daemon=True).start()
        
        return proc.pid, None
        
    except Exception as e:
        log(f"[{ts()}] Error: {str(e)}")
        return None, str(e)

def stop_bot_process(pid):
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except: return False

def monitor_bot(server_id, pid):
    while True:
        try:
            if sys.platform == 'win32':
                result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True)
                if str(pid) not in result.stdout:
                    break
            else:
                try: os.kill(pid, 0)
                except: break
        except: break
        time.sleep(5)
    
    server, _ = get_server_by_id(server_id)
    if not server: return
    if server.get('stopped_by_user'): return
    if server.get('rate_limit_exceeded'): return
    
    if should_auto_restart(server_id):
        time.sleep(3)
        new_pid, error = run_bot(server_id, server.get('main_file', 'main.py'), 
                                 server.get('requirements_file', 'requirements.txt'))
        if new_pid:
            users = load_users()
            for uname, data in users.items():
                if uname == 'admin': continue
                servers = data.get('servers', [])
                if not isinstance(servers, list): continue
                for s in servers:
                    if isinstance(s, dict) and s.get('server_id') == server_id:
                        s['status'] = 'running'
                        s['pid'] = new_pid
                        s['started_at'] = str(datetime.now())
                        s['rate_limit_exceeded'] = False
                        s['stopped_by_user'] = False
                        save_users(users)
                        break
            threading.Thread(target=monitor_bot, args=(server_id, new_pid), daemon=True).start()
    else:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'stopped'
                    s['pid'] = None
                    save_users(users)
                    return

def get_process_stats(pid):
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.5)
        mem = proc.memory_info()
        ram = mem.rss / (1024 * 1024)
        return {
            'cpu_percent': round(cpu, 1),
            'ram_mb': round(ram, 1),
            'ram_display': f"{ram:.1f} MB" if ram < 1024 else f"{ram/1024:.1f} GB",
        }
    except:
        return {'cpu_percent': 0, 'ram_mb': 0, 'ram_display': '0 MB'}

def get_network_stats(psutil_pid):
    try:
        proc = psutil.Process(psutil_pid)
        io = proc.io_counters()
        if io:
            read_kb = io.read_bytes / 1024
            write_kb = io.write_bytes / 1024
            return format_bytes(read_kb), format_bytes(write_kb)
    except: pass
    return "0 KB", "0 KB"

def format_bytes(kb):
    if kb < 1024: return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024: return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"

# ============================================
# Public API - Server Creation
# ============================================

@app.route('/api/create', methods=['GET'])
def api_create_server():
    username = request.args.get('username', '').strip()
    password = request.args.get('password', '').strip()
    server_type = request.args.get('type', 'python').strip()
    ram = request.args.get('ram', '1GB').strip()
    disk = request.args.get('disk', '1GB').strip()
    cpu_limit = int(request.args.get('cpu', '30'))
    days = int(request.args.get('days', '3'))
    
    if not password:
        password = generate_random_password(10)
    
    if not username:
        username = f"VELATRIX_{random.randint(1000, 9999)}"
    
    if len(username) < 3: return jsonify({'status': 'error', 'message': 'Username too short'}), 400
    if len(password) < 4: return jsonify({'status': 'error', 'message': 'Password too short'}), 400
    
    users = load_users()
    if username in users: return jsonify({'status': 'error', 'message': 'Username exists!'}), 400
    
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=days)
    
    create_default_files(get_server_dir(server_id), server_type)
    
    host = request.host
    scheme = 'http' if host.startswith('localhost') or host.startswith('127.') else 'https'
    full_url = f"{scheme}://{host}/{server_id}/login"
    
    new_server = {
        'server_id': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': full_url,
        'type': server_type,
        'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()),
        'expiry': str(expiry_date),
        'main_file': 'index.html' if 'static' in server_type.lower() else ('index.js' if 'node' in server_type.lower() else 'main.py'),
        'requirements_file': '' if 'static' in server_type.lower() else 'requirements.txt',
        'cpu_limit': cpu_limit,
        'rate_limit_exceeded': False,
        'stopped_by_user': False
    }
    
    users[username] = {'password': password, 'role': 'user', 'servers': [new_server]}
    save_users(users)
    
    return jsonify({
        'status': 'success',
        'message': 'Panel created!',
        'username': username,
        'password': password,
        'server_type': server_type,
        'full_url': full_url,
        'server_id': server_id
    }), 200

# ============================================
# Routes
# ============================================

@app.route('/')
def index():
    return render_template('landing.html')

@app.route('/landing')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        if username == 'admin' and password == users.get('admin', {}).get('password'):
            session['user'] = 'admin'
            session['role'] = 'admin'
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html', error="Invalid credentials!")
    return render_template('login.html', error=None)

@app.route('/<server_id>/login', methods=['GET', 'POST'])
def server_login(server_id):
    valid, result = check_server_valid(server_id)
    if not valid: return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    if username == uname and password == data.get('password'):
                        session['user'] = uname
                        session['role'] = 'user'
                        session['current_server_id'] = server_id
                        return redirect(url_for('server_home', server_id=server_id))
        return render_template('login.html', error="Invalid credentials!")
    return render_template('login.html', error=None)

@app.route('/<server_id>/home')
def server_home(server_id):
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))
    if session.get('current_server_id') != server_id:
        session.clear()
        return redirect(url_for('server_login', server_id=server_id))
    
    valid, result = check_server_valid(server_id)
    if not valid:
        session.clear()
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    
    return render_template('home.html', username=session['user'], current_server=result)

@app.route('/logout')
def logout():
    server_id = session.get('current_server_id')
    session.clear()
    if server_id: return redirect(url_for('server_login', server_id=server_id))
    return redirect(url_for('login'))

# ============================================
# Admin
# ============================================

@app.route('/admin')
def admin_dashboard():
    if 'user' not in session or session.get('role') != 'admin': return redirect(url_for('login'))
    users = load_users()
    user_list = []
    total_servers, total_running = 0, 0
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): servers = []
        running = sum(1 for s in servers if isinstance(s, dict) and s.get('status') == 'running')
        total_servers += len(servers)
        total_running += running
        user_list.append({'username': uname, 'password': data.get('password', ''), 'servers': servers, 'server_count': len(servers), 'running_count': running})
    return render_template('admin.html', users=user_list, total_servers=total_servers, total_running=total_running)

@app.route('/admin/create_server', methods=['POST'])
def create_server():
    if 'user' not in session or session.get('role') != 'admin': return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    username, password = data.get('username', ''), data.get('password', '')
    server_type = data.get('server_type', 'python')
    
    if not username or not password: return jsonify({'error': 'Required!'}), 400
    users = load_users()
    server_id = str(uuid.uuid4())[:8]
    
    create_default_files(get_server_dir(server_id), server_type)
    
    new_server = {
        'server_id': server_id, 'link': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': request.host_url.rstrip('/') + f"/{server_id}/home",
        'type': server_type,
        'ram': data.get('ram', '512MB'), 'disk': data.get('disk', '1GB'),
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()),
        'expiry': str(datetime.now() + timedelta(days=int(data.get('expiry_days', 30)))),
        'main_file': 'index.html' if 'static' in server_type.lower() else ('index.js' if 'node' in server_type.lower() else 'main.py'),
        'requirements_file': '' if 'static' in server_type.lower() else 'requirements.txt',
        'cpu_limit': int(data.get('cpu_limit', 80)), 'rate_limit_exceeded': False, 'stopped_by_user': False
    }
    
    if username not in users: users[username] = {'password': password, 'role': 'user', 'servers': []}
    users[username]['servers'].append(new_server)
    save_users(users)
    return jsonify({'success': True, 'hostname': new_server['full_link'], 'server_id': server_id})

@app.route('/admin/delete_server/<username>/<server_id>', methods=['POST'])
def delete_server(username, server_id):
    if 'user' not in session or session.get('role') != 'admin': return jsonify({'error': 'Unauthorized'}), 403
    users = load_users()
    if username in users:
        servers = users[username].get('servers', [])
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('pid'): stop_bot_process(s['pid'])
                try: shutil.rmtree(get_server_dir(server_id))
                except: pass
                break
        users[username]['servers'] = [s for s in servers if isinstance(s, dict) and s.get('server_id') != server_id]
        if not users[username]['servers']: del users[username]
        save_users(users)
    return jsonify({'success': True})

# ============================================
# Bot API
# ============================================

@app.route('/api/run/<server_id>', methods=['POST'])
def api_run(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'})
    if server.get('status') == 'running': return jsonify({'status': 'error', 'msg': 'Already running!'})
    
    server['rate_limit_exceeded'] = False
    server['stopped_by_user'] = False
    
    pid, error = run_bot(server_id, server.get('main_file'), server.get('requirements_file'))
    
    if pid:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            for s in data.get('servers', []):
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'running'
                    s['pid'] = pid
                    s['started_at'] = str(datetime.now())
                    save_users(users)
                    break
        threading.Thread(target=monitor_bot, args=(server_id, pid), daemon=True).start()
        return jsonify({'status': 'success', 'msg': 'Started!'})
    return jsonify({'status': 'error', 'msg': error or 'Failed'})

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'})
    if server.get('pid'): stop_bot_process(server['pid'])
    
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        for s in data.get('servers', []):
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['status'] = 'stopped'
                s['pid'] = None
                s['stopped_by_user'] = True
                save_users(users)
                break
    return jsonify({'status': 'success', 'msg': 'Stopped'})

@app.route('/api/logs/<server_id>')
def api_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    logs = ""
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f: logs = f.read()
        except: pass
    return jsonify({'logs': logs})

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if os.path.exists(log_file): os.remove(log_file)
        return jsonify({'status': 'success', 'msg': 'Cleared'})
    except: return jsonify({'status': 'error'}), 500

@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json()
    cmd, server_id = data.get('cmd', ''), data.get('server_id', '')
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=get_server_dir(server_id), timeout=30)
        return jsonify({'status': 'success', 'output': (result.stdout + result.stderr)[:2000]})
    except: return jsonify({'status': 'error', 'msg': 'Timeout'})

@app.route('/api/stats/<server_id>')
def api_stats(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'cpu': '0%', 'ram': '0 MB', 'uptime': '0h', 'status': 'unknown'})
    
    uptime, cpu, ram, net_in, net_out = "0h 0m", "0%", "0 MB", "0 KB", "0 KB"
    if server.get('status') == 'running' and server.get('pid'):
        stats = get_process_stats(server['pid'])
        cpu, ram = f"{stats['cpu_percent']}%", stats['ram_display']
        net_in, net_out = get_network_stats(server['pid'])
    
    return jsonify({'cpu': cpu, 'ram': ram, 'uptime': uptime, 'net_in': net_in, 'net_out': net_out, 'cpu_limit': server.get('cpu_limit', 80), 'status': server.get('status', 'stopped')})

# ============================================
# Files API
# ============================================

@app.route('/api/files/<server_id>')
def api_files(server_id):
    folder = request.args.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder: server_dir = os.path.join(server_dir, folder)
    if not os.path.exists(server_dir): return jsonify({'files': []})
    
    files = []
    try:
        for item in os.listdir(server_dir):
            p = os.path.join(server_dir, item)
            files.append({'name': item, 'is_dir': os.path.isdir(p), 'size': os.path.getsize(p) if os.path.isfile(p) else 0, 'modified': datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M')})
    except: pass
    return jsonify({'files': files})

@app.route('/api/file/<server_id>', methods=['GET', 'POST', 'DELETE'])
def api_handle_file(server_id):
    if request.method == 'GET':
        p = os.path.join(get_server_dir(server_id), request.args.get('filename', ''))
        if os.path.exists(p) and os.path.isfile(p):
            with open(p, 'r', encoding='utf-8') as f: return jsonify({'content': f.read()})
        return jsonify({'error': 'Not found'}), 404
    elif request.method == 'POST':
        data = request.get_json()
        p = os.path.join(get_server_dir(server_id), data.get('filename', ''))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f: f.write(data.get('content', ''))
        return jsonify({'success': True})
    elif request.method == 'DELETE':
        p = os.path.join(get_server_dir(server_id), request.get_json().get('filename', ''))
        if os.path.exists(p):
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        return jsonify({'success': True})

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    server_dir = os.path.join(get_server_dir(server_id), request.form.get('folder', ''))
    os.makedirs(server_dir, exist_ok=True)
    file.save(os.path.join(server_dir, file.filename))
    return jsonify({'success': True})

@app.route('/api/get_startup/<server_id>')
def api_get_startup(server_id):
    server, _ = get_server_by_id(server_id)
    if server: return jsonify({'main_file': server.get('main_file', ''), 'requirements_file': server.get('requirements_file', '')})
    return jsonify({})

@app.route('/api/set_startup/<server_id>', methods=['POST'])
def api_set_startup(server_id):
    d = request.get_json()
    users = load_users()
    for uname, udata in users.items():
        if uname == 'admin': continue
        for s in udata.get('servers', []):
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['main_file'] = d.get('main_file', '')
                s['requirements_file'] = d.get('requirements_file', '')
                save_users(users)
                return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

if __name__ == '__main__':
    print("=" * 50)
    print("🚀 VELATRIX HOSTING - V2.0")
    print("=" * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
