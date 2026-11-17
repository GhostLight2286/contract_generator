from flask import Flask, render_template, request, send_file, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_RIGHT
from jinja2 import Template
from functools import wraps
import io
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-for-course-project'  # Нужно для работы сессий
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'  # База данных SQLite
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # Куда перенаправлять неавторизованных


# === МОДЕЛИ БАЗЫ ДАННЫХ ===
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'client' или 'notary'


# Создаем базу данных при первом запуске
with app.app_context():
    db.create_all()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Декоратор для проверки ролей
def role_required(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role != role:
                flash('У вас нет доступа к этой странице.', 'danger')
                return redirect(url_for('index'))
            return f(*args, **kwargs)

        return decorated_function

    return decorator


# === РЕГИСТРАЦИЯ ШРИФТА ===
FONT_PATH = os.path.join('static', 'DejaVuSans.ttf')
if os.path.exists(FONT_PATH):
    pdfmetrics.registerFont(TTFont('DejaVuSans', FONT_PATH))
    registerFontFamily('DejaVuSans', normal='DejaVuSans', bold='DejaVuSans', italic='DejaVuSans',
                       boldItalic='DejaVuSans')


# === МАРШРУТЫ АВТОРИЗАЦИИ ===
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')

        user_exists = User.query.filter_by(username=username).first()
        if user_exists:
            flash('Пользователь с таким именем уже существует.', 'danger')
            return redirect(url_for('register'))

        new_user = User(
            username=username,
            password_hash=generate_password_hash(password),
            role=role
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Регистрация успешна! Теперь войдите.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash('Вы успешно вошли!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Неверный логин или пароль.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('login'))


# === ГЛАВНЫЕ МАРШРУТЫ ===
@app.route('/')
@login_required
def index():
    # В зависимости от роли показываем разные страницы
    if current_user.role == 'client':
        return render_template('index.html')
    elif current_user.role == 'notary':
        return redirect(url_for('notary_dashboard'))
    return "Неизвестная роль", 403


@app.route('/notary')
@login_required
@role_required('notary')
def notary_dashboard():
    # Здесь нотариус будет видеть список сгенерированных договоров
    return render_template('notary_dashboard.html')


# === ГЕНЕРАЦИЯ PDF (Только для клиентов) ===
@app.route('/generate', methods=['POST'])
@login_required
@role_required('client')
def generate():
    # ... (Здесь весь ваш код генерации PDF без изменений, который мы писали ранее) ...
    data = request.form.to_dict()
    contract_type = data.get('contract_type')
    template_name = {'rent': 'rent_contract.txt', 'work': 'work_contract.txt', 'gift': 'gift_contract.txt'}.get(
        contract_type)

    with open(os.path.join('templates', template_name), 'r', encoding='utf-8') as f:
        template_content = f.read()

    jinja_template = Template(template_content)
    rendered_text = jinja_template.render(**data)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=30 * mm, rightMargin=10 * mm, topMargin=20 * mm,
                            bottomMargin=20 * mm)

    style_normal = ParagraphStyle(name='Normal', fontName='DejaVuSans', fontSize=14, leading=18, alignment=TA_JUSTIFY,
                                  firstLineIndent=25, spaceAfter=6)
    style_center = ParagraphStyle(name='Center', parent=style_normal, alignment=TA_CENTER, firstLineIndent=0,
                                  spaceAfter=12)
    style_no_indent = ParagraphStyle(name='NoIndent', parent=style_normal, firstLineIndent=0)
    style_right = ParagraphStyle(name='Right', parent=style_no_indent, alignment=TA_RIGHT)

    story = []
    lines = rendered_text.split('\n')

    for i, line in enumerate(lines):
        line = line.strip()
        if i == 0:
            story.append(Paragraph(f"<b>{line}</b>", style_center))
            continue
        if i == 2 and "{{" not in line and len(line.split()) > 1:
            parts = line.split(maxsplit=1)
            city, date_str = parts[0], parts[1] if len(parts) > 1 else ""
            data_table = [[Paragraph(city, style_no_indent), Paragraph(date_str, style_right)]]
            t = Table(data_table, colWidths=[doc.width / 2, doc.width / 2])
            t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
            story.append(t)
            story.append(Spacer(1, 12))
            continue
        if not line:
            story.append(Spacer(1, 10))
            continue
        if line.isupper() and len(line) < 60:
            story.append(Spacer(1, 10))
            story.append(Paragraph(f"<b>{line}</b>", style_no_indent))
            continue
        story.append(Paragraph(line, style_normal))

    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True, download_name=f"Договор_{contract_type}.pdf",
                     mimetype='application/pdf')


if __name__ == '__main__':
    app.run(debug=True)