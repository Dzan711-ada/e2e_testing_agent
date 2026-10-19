"""Small isolated acceptance environments. The core never imports this module."""
from contextlib import contextmanager
import threading
from flask import Flask, request
from werkzeug.serving import make_server


@contextmanager
def demo_server(port=0):
    app = Flask(__name__)

    @app.get('/')
    def index():
        return '<h1>测试环境</h1><a href="/identity">账号中心</a><a href="/orders">订单查询</a><a href="/newsletter">订阅周刊</a>'

    @app.get('/identity')
    def identity():
        return '<h1>登录</h1><p>支持两种登录方式</p><a href="/identity/password">密码登录</a><a href="/identity/email-code">邮箱验证码登录</a>'

    @app.route('/identity/password', methods=['GET', 'POST'])
    def password():
        if request.method == 'POST':
            if request.form.get('account') == 'demo-user' and request.form.get('password') == 'demo-password':
                return '<h1>个人中心</h1><p>登录成功</p>'
            return '<h1>登录未成功</h1><p role="alert">账号或密码错误</p>'
        return '<h1>密码登录</h1><form method="post"><label>账号<input name="account"></label><label>密码<input name="password" type="password"></label><button>登录</button></form>'

    @app.route('/identity/email-code', methods=['GET', 'POST'])
    def email_code():
        if request.method == 'POST':
            return '<h1>个人中心</h1><p>登录成功</p>' if request.form.get('code') == '135790' else '<p role="alert">验证码错误</p>'
        return '''<h1>邮箱验证码登录</h1><form method="post"><label>邮箱<input name="email" type="email"></label>
        <button type="button" onclick="fetch('/identity/send-code',{method:'POST'}).then(r=>r.text()).then(t=>document.querySelector('output').textContent=t)">获取验证码</button>
        <output role="status"></output><label>验证码<input name="code"></label><button>登录</button></form>'''

    @app.post('/identity/send-code')
    def send_code():
        print('本地模拟验证码：135790（仅测试环境，无真实邮件）', flush=True)
        return '模拟验证码已发送，请查看测试站控制台'

    @app.route('/orders', methods=['GET', 'POST'])
    def orders():
        if request.method == 'POST':
            return '<h1>查询结果</h1><p role="status">已发货</p>'
        return '<h1>订单查询</h1><form method="post"><input aria-label="订单编号" name="order"><button>查询</button></form>'

    @app.route('/newsletter', methods=['GET', 'POST'])
    def newsletter():
        if request.method == 'POST':
            return '<h1>订阅结果</h1><p role="status">订阅成功</p>'
        return '<h1>订阅周刊</h1><form method="post"><label>收件邮箱<input type="email" name="email"></label><button>订阅</button></form>'

    server = make_server('127.0.0.1', port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join(timeout=5)
