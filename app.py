from flask import Flask, render_template, request, send_file, redirect, url_for, flash, abort
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
from reportlab.pdfgen import canvas as pdf_canvas
from pypdf import PdfReader, PdfWriter
from jinja2 import Template
from functools import wraps
from datetime import datetime
import io
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-for-course-project'  # Нужно для работы сессий
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'database.db')
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


class Contract(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref='contracts')
    contract_type = db.Column(db.String(20), nullable=False)       # rent / work / gift
    contract_number = db.Column(db.String(50), default='')
    status = db.Column(db.String(20), nullable=False, default='draft')
    # Статусы: draft -> отправлен клиентом; pending -> на проверке у нотариуса;
    #           approved -> одобрен (есть финальный PDF с печатью и подписью);
    #           rejected -> отклонён (нужно переделать, есть комментарий нотариуса)
    original_pdf = db.Column(db.String(200))      # файл черновика, сгенерированного клиентом
    signed_pdf = db.Column(db.String(200))        # финальный файл с подписью и печатью нотариуса
    comment = db.Column(db.Text)                  # замечания нотариуса при отклонении
    form_data_json = db.Column(db.Text)           # сохранённые данные формы (для редактирования после отклонения)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def form_data(self):
        return json.loads(self.form_data_json) if self.form_data_json else {}


TYPE_NAMES = {'rent': 'Договор аренды', 'work': 'Договор подряда', 'gift': 'Договор дарения'}
STATUS_NAMES = {'draft': 'Черновик', 'pending': 'На проверке', 'approved': 'Одобрен', 'rejected': 'Отклонён (нужно переделать)'}

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
FONT_PATH = os.path.join(BASE_DIR, 'static', 'DejaVuSans.ttf')
if os.path.exists(FONT_PATH):
    pdfmetrics.registerFont(TTFont('DejaVuSans', FONT_PATH))
    registerFontFamily('DejaVuSans', normal='DejaVuSans', bold='DejaVuSans', italic='DejaVuSans',
                       boldItalic='DejaVuSans')

SIGNATURE_PATH = os.path.join(BASE_DIR, 'static', 'signature.png')
STAMP_PATH = os.path.join(BASE_DIR, 'static', 'stamp.png')


# === ГЕНЕРАЦИЯ PDF ДОГОВОРА ===
def build_contract_pdf(template_name, data):
    """Рендерит шаблон договора текстом и возвращает PDF в памяти (BytesIO)."""
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
    return buffer


def stamp_and_sign_pdf(original_path, signed_path, notary_username):
    """Накладывает подпись и печать нотариуса на последнюю страницу договора."""
    packet = io.BytesIO()
    c = pdf_canvas.Canvas(packet, pagesize=A4)
    page_w, page_h = A4

    # Подпись нотариуса (в области подписи, левая часть нижней части страницы)
    if os.path.exists(SIGNATURE_PATH):
        c.drawImage(SIGNATURE_PATH, x=page_w * 0.08, y=page_h * 0.10,
                    width=120, height=50, mask='auto', preserveAspectRatio=True)
    c.setFont('DejaVuSans', 9)
    c.drawString(page_w * 0.08, page_h * 0.075, f"Подпись нотариуса: {notary_username}")

    # Печать нотариуса (правая часть нижней части страницы)
    if os.path.exists(STAMP_PATH):
        c.drawImage(STAMP_PATH, x=page_w * 0.60, y=page_h * 0.07,
                    width=130, height=130, mask='auto', preserveAspectRatio=True)
    c.setFont('DejaVuSans', 9)
    c.drawCentredString(page_w * 0.60 + 65, page_h * 0.055, "Печать нотариуса")

    c.save()
    packet.seek(0)

    overlay_reader = PdfReader(packet)
    overlay_page = overlay_reader.pages[-1]

    reader = PdfReader(original_path)
    writer = PdfWriter()
    for page in reader.pages:
        page.merge_page(overlay_page)
        writer.add_page(page)

    with open(signed_path, 'wb') as f:
        writer.write(f)


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
        contracts = Contract.query.filter_by(user_id=current_user.id).order_by(Contract.id.desc()).all()
        return render_template('index.html', contracts=contracts,
                               type_names=TYPE_NAMES, status_names=STATUS_NAMES)
    elif current_user.role == 'notary':
        return redirect(url_for('notary_dashboard'))
    return "Неизвестная роль", 403


@app.route('/notary')
@login_required
@role_required('notary')
def notary_dashboard():
    # Нотариус видит договоры на проверке, а также историю уже рассмотренных
    pending = Contract.query.filter_by(status='pending').order_by(Contract.created_at.desc()).all()
    reviewed = Contract.query.filter(Contract.status.in_(['approved', 'rejected'])) \
        .order_by(Contract.updated_at.desc()).all()
    return render_template('notary_dashboard.html', pending=pending, reviewed=reviewed,
                           type_names=TYPE_NAMES, status_names=STATUS_NAMES)


# === КЛИЕНТ: ГЕНЕРАЦИЯ И ОТПРАВКА НОТАРИУСУ ===
@app.route('/generate', methods=['POST'])
@login_required
@role_required('client')
def generate():
    data = request.form.to_dict()
    contract_type = data.get('contract_type')
    template_name = {'rent': 'rent_contract.txt', 'work': 'work_contract.txt', 'gift': 'gift_contract.txt'}.get(
        contract_type)
    if not template_name:
        flash('Неизвестный тип договора.', 'danger')
        return redirect(url_for('index'))

    buffer = build_contract_pdf(template_name, data)

    # Сохраняем договор в базу и на диск как черновик
    contract = Contract(user_id=current_user.id, contract_type=contract_type,
                        contract_number=data.get('contract_number', ''), status='draft',
                        form_data_json=json.dumps(data, ensure_ascii=False))
    db.session.add(contract)
    db.session.commit()

    filename = f"contract_{contract.id}_original.pdf"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, 'wb') as f:
        f.write(buffer.getvalue())
    contract.original_pdf = filename
    db.session.commit()

    action = data.get('action', 'save')
    if action == 'send':
        contract.status = 'pending'
        db.session.commit()
        flash(f'{TYPE_NAMES[contract_type]} №{contract.contract_number} отправлен нотариусу на проверку.', 'success')
        return redirect(url_for('index'))

    # Просто скачать черновик, не отправляя
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Договор_{contract_type}.pdf",
                     mimetype='application/pdf')


def get_own_contract_or_404(contract_id):
    contract = Contract.query.get_or_404(contract_id)
    if contract.user_id != current_user.id:
        abort(403)
    return contract


@app.route('/contract/<int:contract_id>/resend', methods=['POST'])
@login_required
@role_required('client')
def resend_contract(contract_id):
    """После отклонения клиент правит данные на форме и перегенерирует документ, затем снова отправляет."""
    contract = get_own_contract_or_404(contract_id)
    data = request.form.to_dict()
    template_name = {'rent': 'rent_contract.txt', 'work': 'work_contract.txt', 'gift': 'gift_contract.txt'}.get(
        contract.contract_type)
    buffer = build_contract_pdf(template_name, data)

    filepath = os.path.join(UPLOAD_FOLDER, contract.original_pdf)
    with open(filepath, 'wb') as f:
        f.write(buffer.getvalue())

    contract.contract_number = data.get('contract_number', contract.contract_number)
    contract.form_data_json = json.dumps(data, ensure_ascii=False)
    contract.status = 'pending'
    contract.comment = None
    db.session.commit()
    flash(f'{TYPE_NAMES[contract.contract_type]} №{contract.contract_number} повторно отправлен нотариусу.', 'success')
    return redirect(url_for('index'))


@app.route('/contract/<int:contract_id>/download')
@login_required
@role_required('client')
def download_contract(contract_id):
    contract = get_own_contract_or_404(contract_id)
    if contract.status == 'approved' and contract.signed_pdf:
        path = os.path.join(UPLOAD_FOLDER, contract.signed_pdf)
        name = f"Договор_{contract.contract_type}_{contract.contract_number}_заверенный.pdf"
    elif contract.original_pdf:
        path = os.path.join(UPLOAD_FOLDER, contract.original_pdf)
        name = f"Договор_{contract.contract_type}_{contract.contract_number}.pdf"
    else:
        abort(404)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name, mimetype='application/pdf')


# === НОТАРИУС: ПРОСМОТР, ОДОБРЕНИЕ, ОТКЛОНЕНИЕ ===
def get_contract_for_notary(contract_id):
    if current_user.role != 'notary':
        abort(403)
    return Contract.query.get_or_404(contract_id)


@app.route('/notary/contract/<int:contract_id>/view')
@login_required
@role_required('notary')
def view_contract(contract_id):
    """Просмотр договора нотариусом (открывается в новой вкладке)."""
    contract = get_contract_for_notary(contract_id)
    path = os.path.join(UPLOAD_FOLDER, contract.original_pdf)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype='application/pdf')


@app.route('/notary/contract/<int:contract_id>/download')
@login_required
@role_required('notary')
def download_contract_view(contract_id):
    """Скачивание договора нотариусом для проверки."""
    contract = get_contract_for_notary(contract_id)
    if contract.original_pdf:
        path = os.path.join(UPLOAD_FOLDER, contract.original_pdf)
        name = f"Договор_{contract.contract_type}_{contract.contract_number}.pdf"
    else:
        abort(404)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name, mimetype='application/pdf')


@app.route('/notary/contract/<int:contract_id>/approve', methods=['POST'])
@login_required
@role_required('notary')
def approve_contract(contract_id):
    contract = get_contract_for_notary(contract_id)
    if contract.status != 'pending':
        flash('Этот документ уже был рассмотрен ранее.', 'warning')
        return redirect(url_for('notary_dashboard'))

    original_path = os.path.join(UPLOAD_FOLDER, contract.original_pdf)
    signed_name = f"contract_{contract.id}_signed.pdf"
    signed_path = os.path.join(UPLOAD_FOLDER, signed_name)

    try:
        stamp_and_sign_pdf(original_path, signed_path, current_user.username)
    except Exception as e:
        flash(f'Ошибка при заверении документа: {e}', 'danger')
        return redirect(url_for('notary_dashboard'))

    contract.signed_pdf = signed_name
    contract.status = 'approved'
    contract.comment = None
    db.session.commit()
    flash(f'{TYPE_NAMES[contract.contract_type]} №{contract.contract_number} одобрен: поставлены печать и подпись, '
          f'документ отправлен клиенту.', 'success')
    return redirect(url_for('notary_dashboard'))


@app.route('/notary/contract/<int:contract_id>/reject', methods=['POST'])
@login_required
@role_required('notary')
def reject_contract(contract_id):
    contract = get_contract_for_notary(contract_id)
    if contract.status != 'pending':
        flash('Этот документ уже был рассмотрен ранее.', 'warning')
        return redirect(url_for('notary_dashboard'))

    comment = (request.form.get('comment') or '').strip()
    if not comment:
        flash('Укажите клиенту, что нужно переделать в документе.', 'danger')
        return redirect(url_for('notary_dashboard'))

    contract.status = 'rejected'
    contract.comment = comment
    contract.signed_pdf = None
    db.session.commit()
    flash(f'{TYPE_NAMES[contract.contract_type]} №{contract.contract_number} отклонён. Клиенту отправлено '
          f'сообщение с замечаниями.', 'info')
    return redirect(url_for('notary_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)
