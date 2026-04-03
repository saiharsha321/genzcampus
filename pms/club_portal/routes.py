from flask import render_template, redirect, url_for, flash, request, jsonify, current_app, session, send_file
from flask_login import login_user, logout_user, login_required, current_user
from club_portal import club_portal as bp
from models import db, Club, ClubMember, Event, EventForm, EventResponse, Team, TeamMember, Attendance, FinanceTransaction, User
from utils import allowed_file
import json
import qrcode
import io
import base64
from datetime import datetime, timedelta
import pandas as pd

# Helper function for Club Portal RBAC
def club_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'club_id' not in session and not current_user.is_authenticated:
            return redirect(url_for('club_portal.login'))
        return f(*args, **kwargs)
    return decorated_function

@bp.context_processor
def inject_club():
    if 'club_id' in session:
        club = Club.query.get(session['club_id'])
        return dict(club=club)
    return dict()

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_id = request.form.get('login_id')
        password = request.form.get('password')
        
        club = Club.query.filter_by(club_login_id=login_id).first()
        if club and club.check_password(password):
            if not club.is_active:
                flash('This club portal has been deactivated by the admin.', 'danger')
                return redirect(url_for('club_portal.login'))
            
            # Correct: set session only when login is valid and club is active
            session['club_id'] = club.id
            session['club_name'] = club.name
            session['role'] = 'President'
            flash(f'Welcome to {club.name} Portal!', 'success')
            return redirect(url_for('club_portal.dashboard'))
        
        flash('Invalid Club ID or Password. Please try again.', 'danger')
    return render_template('club/login.html')

@bp.route('/dashboard')
def dashboard():
    club_id = session.get('club_id')
    if not club_id:
        return redirect(url_for('club_portal.login'))
    
    club = Club.query.get(club_id)
    events = Event.query.filter_by(club_id=club_id).order_by(Event.start_date.desc()).all()
    
    # Update event statuses automatically
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    for event in events:
        if event.end_date and now > event.end_date:
            event.status = 'expired'
        elif event.start_date and now >= event.start_date and (not event.end_date or now <= event.end_date):
            event.status = 'active'
        else:
            event.status = 'upcoming'
    db.session.commit()

    return render_template('club/dashboard.html', club=club, events=events, now=now)

@bp.route('/logout')
def logout():
    session.pop('club_id', None)
    session.pop('role', None)
    return redirect(url_for('club_portal.login'))

@bp.route('/events/create', methods=['GET', 'POST'])
def create_event():
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))

    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%dT%H:%M')
        end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%dT%H:%M')
        venue = request.form.get('venue')
        participation_type = request.form.get('participation_type') # solo, team, both
        allowed_depts = request.form.getlist('departments') # Multi-select
        registration_deadline_str = request.form.get('registration_deadline')
        registration_deadline = datetime.strptime(registration_deadline_str, '%Y-%m-%dT%H:%M') if registration_deadline_str else None
        
        event = Event(
            club_id=club_id,
            name=name,
            description=description,
            date=start_date.date(), # Keep legacy field sync
            start_date=start_date,
            end_date=end_date,
            registration_deadline=registration_deadline,
            venue=venue,
            participation_type=participation_type,
            allowed_departments=json.dumps(allowed_depts),
            status='upcoming'
        )
        db.session.add(event)
        db.session.commit()
        
        # Initialize empty form schema
        form = EventForm(event_id=event.id, schema_json=json.dumps([]))
        db.session.add(form)
        db.session.commit()
        
        flash('Event created successfully. Now build your application form.', 'success')
        return redirect(url_for('club_portal.build_form', event_id=event.id))

    from models import Department
    departments = Department.query.all()
    return render_template('club/create_event.html', departments=departments)

@bp.route('/events/<int:event_id>/build-form', methods=['GET', 'POST'])
def build_form(event_id):
    event = Event.query.get_or_404(event_id)
    form_obj = EventForm.query.filter_by(event_id=event_id).first()
    
    if request.method == 'POST':
        # Expecting JSON schema from frontend form builder
        schema = request.form.get('schema_json')
        form_obj.schema_json = schema
        db.session.commit()
        flash('Form structure saved!', 'success')
        return redirect(url_for('club_portal.dashboard'))
        
    return render_template('club/build_form.html', event=event, form_obj=form_obj)

@bp.route('/events/<int:event_id>/update-deadline', methods=['POST'])
def update_deadline(event_id):
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    
    event = Event.query.get_or_404(event_id)
    if event.club_id != club_id:
        flash('Unauthorized action.', 'danger')
        return redirect(url_for('club_portal.dashboard'))
    
    new_deadline_str = request.form.get('registration_deadline')
    if new_deadline_str:
        try:
            new_deadline = datetime.strptime(new_deadline_str, '%Y-%m-%dT%H:%M')
            event.registration_deadline = new_deadline
            db.session.commit()
            flash(f'Registration deadline for "{event.name}" updated successfully!', 'success')
        except ValueError:
            flash('Invalid date format.', 'danger')
    
    return redirect(url_for('club_portal.dashboard'))

@bp.route('/events/<int:event_id>/delete')
def delete_event(event_id):
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    
    event = Event.query.get_or_404(event_id)
    
    # Security check
    if event.club_id != club_id:
        flash('Unauthorized action.', 'danger')
        return redirect(url_for('club_portal.dashboard'))
    
    # Check for linked permissions or responses
    if event.permissions or event.responses:
        flash('Cannot delete event that has registrations or permission requests.', 'danger')
        return redirect(url_for('club_portal.dashboard'))

    # Delete associated form if exists
    if event.form:
        db.session.delete(event.form)
    
    db.session.delete(event)
    db.session.commit()
    
    flash('Event deleted successfully.', 'success')
    return redirect(url_for('club_portal.dashboard'))

# --- Student Facing Routes ---

@bp.route('/available-events')
@login_required
def student_events():
    if not current_user.is_student():
        flash('Only students can access this page', 'danger')
        return redirect(url_for('index'))
    
    # Show ALL events (Active, Upcoming, and Past)
    all_events = Event.query.order_by(Event.start_date.desc()).all()
    
    available_events = []
    user_dept = current_user.department.strip().lower() if current_user.department else ""
    
    for event in all_events:
        allowed = json.loads(event.allowed_departments) if event.allowed_departments else []
        # Case-insensitive, stripped check
        if not allowed or any(d.strip().lower() == user_dept for d in allowed):
            available_events.append(event)
            
    # Check if student already registered
    registered_event_ids = [r.event_id for r in EventResponse.query.filter_by(student_id=current_user.id).all()]
            
    return render_template('club/student_events.html', events=available_events, registered_ids=registered_event_ids)

@bp.route('/events/<int:event_id>/apply', methods=['GET', 'POST'])
@login_required
def apply_event(event_id):
    event = Event.query.get_or_404(event_id)
    form_obj = EventForm.query.filter_by(event_id=event_id).first()
    
    if not event.is_registration_open:
        flash('Registrations for this event are closed.', 'danger')
        return redirect(url_for('club_portal.student_events'))

    # Check department visibility
    allowed = json.loads(event.allowed_departments) if event.allowed_departments else []
    if allowed and current_user.department not in allowed:
        flash('Your department is not eligible for this event.', 'danger')
        return redirect(url_for('club_portal.student_events'))

    # Check if already registered
    existing = EventResponse.query.filter_by(event_id=event_id, student_id=current_user.id).first()
    if existing:
        flash('You have already registered for this event.', 'warning')
        return redirect(url_for('club_portal.registration_ticket', response_id=existing.id))

    if request.method == 'POST':
        # participation_type handling
        team_id = None
        if event.participation_type in ['team', 'both']:
            ptype = request.form.get('participation_choice')
            if ptype == 'team':
                team_action = request.form.get('team_action') # create or join
                if team_action == 'create':
                    t_name = request.form.get('team_name')
                    if not t_name:
                        flash('Team name is required to create a team.', 'danger')
                        return redirect(request.url)
                    
                    import random, string
                    t_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                    
                    new_team = Team(event_id=event_id, leader_id=current_user.id, team_name=t_name, team_id_code=t_code)
                    db.session.add(new_team)
                    db.session.flush()
                    team_id = new_team.id
                    
                    # Add leader as member
                    leader_mem = TeamMember(team_id=team_id, user_id=current_user.id, role='leader')
                    db.session.add(leader_mem)
                
                elif team_action == 'join':
                    t_code = request.form.get('team_code')
                    target_team = Team.query.filter_by(event_id=event_id, team_id_code=t_code).first()
                    if not target_team:
                        flash('Invalid Team Code for this event.', 'danger')
                        return redirect(request.url)
                    
                    # Check if team is full
                    if len(target_team.members) >= event.team_size_max:
                        flash('This team is already full.', 'danger')
                        return redirect(request.url)
                    
                    team_id = target_team.id
                    # Add as member
                    new_mem = TeamMember(team_id=team_id, user_id=current_user.id, role='member')
                    db.session.add(new_mem)

        # Collect custom form data
        schema = json.loads(form_obj.schema_json)
        responses = {}
        for field in schema:
            val = request.form.get(f"field_{field['label']}")
            if field.get('required') and not val:
                flash(f"{field['label']} is required", 'danger')
                return redirect(request.url)
            responses[field['label']] = val

        # Generate unique ticket ID
        import uuid
        ticket_id = f"GRD-TKT-{uuid.uuid4().hex[:8].upper()}"
        
        # QR Code Generation
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(ticket_id)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()

        response = EventResponse(
            event_id=event_id,
            student_id=current_user.id,
            response_json=json.dumps(responses),
            ticket_id=ticket_id,
            qr_code_data=qr_base64
        )
        db.session.add(response)
        db.session.commit()
        
        flash('Registration successful!', 'success')
        return redirect(url_for('club_portal.registration_ticket', response_id=response.id))

    schema = json.loads(form_obj.schema_json) if form_obj else []
    return render_template('club/apply_event.html', event=event, schema=schema, now=datetime.utcnow())

@bp.route('/my-registrations/<int:response_id>')
@login_required
def registration_ticket(response_id):
    response = EventResponse.query.get_or_404(response_id)
    if response.student_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))
    return render_template('club/ticket.html', response=response)

@bp.route('/scanner')
def scanner():
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    return render_template('club/scanner.html')

@bp.route('/mark-attendance', methods=['POST'])
def mark_attendance():
    club_id = session.get('club_id')
    if not club_id: return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    data = request.get_json()
    ticket_id = data.get('ticket_id')
    session_type = data.get('session_type', 'Default')
    
    response = EventResponse.query.filter_by(ticket_id=ticket_id).first()
    if not response:
        return jsonify({'success': False, 'message': 'Invalid Ticket ID'}), 404
        
    # Check if this student belongs to an event of this club
    if response.event.club_id != club_id:
        return jsonify({'success': False, 'message': 'Ticket does not belong to this club'}), 403
        
    # Check if already marked for this session
    existing = Attendance.query.filter_by(response_id=response.id, session_type=session_type).first()
    if existing:
        return jsonify({'success': False, 'message': f'Attendance already marked for {session_type}'}), 400
        
    # Mark attendance
    attendance = Attendance(
        response_id=response.id,
        session_type=session_type,
        scanned_by=session.get('user_id') # If individual login is used
    )
    db.session.add(attendance)
    db.session.commit()
    
    student = response.student
    return jsonify({
        'success': True, 
        'message': f'Attendance marked for {student.first_name} {student.last_name}',
        'student_info': {
            'name': student.get_full_name(),
            'roll_no': student.roll_no,
            'dept': student.department
        }
    })

@bp.route('/finances', methods=['GET', 'POST'])
def finances():
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    
    club = Club.query.get(club_id)
    if request.method == 'POST':
        amount = float(request.form.get('amount'))
        trans_type = request.form.get('type') # credit, debit
        category = request.form.get('category')
        description = request.form.get('description')
        
        # Update club balance
        if trans_type == 'credit':
            club.balance += amount
        else:
            club.balance -= amount
            
        transaction = FinanceTransaction(
            club_id=club_id,
            amount=amount,
            type=trans_type,
            category=category,
            description=description
        )
        db.session.add(transaction)
        db.session.commit()
        flash('Transaction recorded successfully', 'success')
        return redirect(url_for('club_portal.finances'))
        
    transactions = FinanceTransaction.query.filter_by(club_id=club_id).order_by(FinanceTransaction.created_at.desc()).all()
    return render_template('club/finances.html', club=club, transactions=transactions)

@bp.route('/events/<int:event_id>/export')
def export_participants(event_id):
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    
    event = Event.query.get_or_404(event_id)
    if event.club_id != club_id: return "Unauthorized", 403
    
    responses = EventResponse.query.filter_by(event_id=event_id).all()
    
    data = []
    for r in responses:
        row = {
            'Ticket ID': r.ticket_id,
            'Roll No': r.student.roll_no,
            'Name': r.student.get_full_name(),
            'Department': r.student.department,
            'Section': r.student.section,
            'Registration Date': r.submitted_at.strftime('%Y-%m-%d %H:%M') if r.submitted_at else 'N/A'
        }
        # Add custom form fields
        if r.response_json:
            custom_data = json.loads(r.response_json)
            row.update(custom_data)
        data.append(row)
        
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Participants')
    output.seek(0)
    
    return send_file(output, download_name=f"{event.name}_participants.xlsx", as_attachment=True)

@bp.route('/events/<int:event_id>/participants')
def view_participants(event_id):
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    
    event = Event.query.get_or_404(event_id)
    if event.club_id != club_id: return "Unauthorized", 403
    
    responses = EventResponse.query.filter_by(event_id=event_id).all()
    
    # Pre-parse JSON for template
    for r in responses:
        r.custom_data = json.loads(r.response_json) if r.response_json else {}
        
    return render_template('club/participants.html', event=event, responses=responses)
