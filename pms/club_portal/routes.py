
from flask import render_template, redirect, url_for, flash, request, session, jsonify, send_file
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

def filter_accepted_responses(event, responses, now):
    """
    Filters out participants who are 'Rejected' (Team-only event, past deadline, size < min).
    """
    if event.participation_type != 'team':
        return responses # Solo/Both allow fallback to individual
        
    deadline = event.registration_deadline or event.start_date or datetime.combine(event.date, datetime.min.time())
    if now <= deadline:
        return responses # Only filter AFTER the deadline
        
    # Get all teams for this event
    teams = {t.id: t for t in Team.query.filter_by(event_id=event.id).all()}
    
    accepted = []
    team_member_counts = {}
    
    # Pre-count members for efficiency
    for t_id in teams.keys():
        team_member_counts[t_id] = TeamMember.query.filter_by(team_id=t_id).count()
        
    for r in responses:
        if not r.team_id:
            # Should not happen in 'team-only' but if it does, it's rejected as solo
            continue
            
        count = team_member_counts.get(r.team_id, 0)
        if count >= event.team_size_min:
            accepted.append(r)
            
    return accepted

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
            
        # Filter accepted responses for counts
        event.accepted_responses = filter_accepted_responses(event, event.responses, now)
        
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
        team_size_min = int(request.form.get('team_size_min', 1))
        team_size_max = int(request.form.get('team_size_max', 1))
        max_registrations = int(request.form.get('max_registrations', 0))
        allowed_depts = request.form.getlist('departments') # Multi-select
        registration_deadline_str = request.form.get('registration_deadline')
        registration_deadline = datetime.strptime(registration_deadline_str, '%Y-%m-%dT%H:%M') if registration_deadline_str else None
        
        # Validation: Deadline must be on or before start_date
        event_start_cmp = start_date or datetime.combine(start_date.date(), datetime.min.time())
        if registration_deadline and registration_deadline > event_start_cmp:
            flash('Registration deadline cannot be after the event start date.', 'danger')
            return redirect(url_for('club_portal.create_event'))

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
            team_size_min=team_size_min,
            team_size_max=team_size_max,
            max_registrations=max_registrations,
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
    if not club_id:
        return redirect(url_for('club_portal.login'))

    event = Event.query.get_or_404(event_id)
    if event.club_id != club_id:
        flash('Unauthorized action.', 'danger')
        return redirect(url_for('club_portal.dashboard'))

    new_deadline_str = request.form.get('registration_deadline')
    if new_deadline_str:
        try:
            new_deadline = datetime.strptime(new_deadline_str, '%Y-%m-%dT%H:%M')
            
            # Validation: Deadline must be on or before start_date
            event_start_cmp = event.start_date or (datetime.combine(event.date, datetime.min.time()) if event.date else None)
            if event_start_cmp and new_deadline > event_start_cmp:
                msg = 'Registration deadline cannot be after the event start date.'
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': False, 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('club_portal.dashboard'))

            event.registration_deadline = new_deadline
            db.session.commit()
            # AJAX response
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                formatted = new_deadline.strftime('%d %b, %H:%M')
                return jsonify({
                    'success': True,
                    'deadline': formatted,
                    'message': f'Registration deadline for "{event.name}" updated successfully!'
                })
            flash(f'Registration deadline for "{event.name}" updated successfully!', 'success')
        except ValueError:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Invalid date format.'}), 400
            flash('Invalid date format.', 'danger')
    # Non-AJAX fallback
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
    def get_dept_code(dept_name):
        if not dept_name: return "GEN"
        d = str(dept_name).strip().upper()
        if 'AIML' in d: return 'CSM'
        if 'CSE-CS' in d or 'CS' in d: return 'CS'
        if 'CSE-DS' in d or 'DS' in d: return 'DS'
        if 'CSE' in d: return 'CSE'
        if 'CIVIL' in d: return 'CIV'
        return d[:3] # Fallback to first 3 chars

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

    # Initial check (handled inside POST for teams with extra members)
    if not request.method == 'POST' and event.max_registrations > 0:
        current_reg_count = EventResponse.query.filter_by(event_id=event_id).count()
        if current_reg_count >= event.max_registrations:
            flash(f'Registration limit of {event.max_registrations} has been reached for this event.', 'danger')
            return redirect(url_for('club_portal.student_events'))

    if request.method == 'POST':
        # Logging for debugging
        debug_info = f"{datetime.now()}: POST Submit. Event {event_id}, User {current_user.id}, Form: {dict(request.form)}\n"
        with open('instance/registration_debug.log', 'a') as f:
            import os
            if not os.path.exists('instance'): os.makedirs('instance')
            f.write(debug_info)

        team_id = None
        extra_members_found = []
        # Get from form
        ptype = request.form.get('participation_choice', 'solo')
        
        # Calculate slots needed for limit check
        reg_slots_needed = 1
        if ptype == 'team' and request.form.get('team_action') == 'create':
            try:
                dec_size = int(request.form.get('declared_size', 1))
                extra_cnt = 0
                for i in range(1, dec_size):
                    if request.form.get(f'member_roll_no_{i}'):
                        extra_cnt += 1
                reg_slots_needed = 1 + extra_cnt
            except: pass

        if event.max_registrations > 0:
            current_reg_count = EventResponse.query.filter_by(event_id=event_id).count()
            if (current_reg_count + reg_slots_needed) > event.max_registrations:
                flash(f'Limit reached. Only {event.max_registrations - current_reg_count} slots left, but you need {reg_slots_needed}.', 'danger')
                return redirect(request.url)

        if event.participation_type in ['team', 'both'] and ptype == 'team':
            team_action = request.form.get('team_action') # create or join
            if team_action == 'create':
                t_name = request.form.get('team_name')
                declared_size_str = request.form.get('declared_size')
                
                if not t_name:
                    flash('Team name is required to create a team.', 'danger')
                    return redirect(request.url)
                
                existing_t = Team.query.filter(Team.event_id==event_id, Team.team_name.ilike(t_name)).first()
                if existing_t:
                    flash('This Team Name is already taken for this event.', 'danger')
                    return redirect(request.url)
                    
                try:
                    declared_size = int(declared_size_str)
                except (ValueError, TypeError):
                    declared_size = event.team_size_min
                
                if declared_size < event.team_size_min or declared_size > event.team_size_max:
                    flash(f'Team size must be between {event.team_size_min} and {event.team_size_max}', 'danger')
                    return redirect(request.url)
                
                import random, string
                rand_num = ''.join(random.choices(string.digits, k=4))
                t_code = (t_name[:4].upper() + rand_num)[:10]
                
                new_team = Team(event_id=event_id, leader_id=current_user.id, team_name=t_name, team_id_code=t_code, declared_size=declared_size)
                db.session.add(new_team)
                db.session.flush()
                team_id = new_team.id
                
                leader_mem = TeamMember(team_id=team_id, user_id=current_user.id, role='leader')
                db.session.add(leader_mem)
                
                for i in range(1, declared_size):
                    roll_no = request.form.get(f'member_roll_no_{i}')
                    if roll_no:
                        roll_no = roll_no.strip().upper()
                        user_mem = User.query.filter_by(roll_no=roll_no).first()
                        if not user_mem:
                            flash(f'Student with Roll Number {roll_no} not found. They must sign up first.', 'danger')
                            db.session.rollback()
                            return redirect(request.url)
                        
                        existing_mem = EventResponse.query.filter_by(event_id=event_id, student_id=user_mem.id).first()
                        if existing_mem:
                            flash(f'Student {roll_no} is already registered for this event.', 'danger')
                            db.session.rollback()
                            return redirect(request.url)
                            
                        new_mem = TeamMember(team_id=team_id, user_id=user_mem.id, role='member')
                        db.session.add(new_mem)
                        extra_members_found.append(user_mem)

            elif team_action == 'join':
                t_code_or_name = request.form.get('team_code', '').strip()
                if not t_code_or_name:
                    flash('Team Code or Name is required.', 'danger')
                    return redirect(request.url)
                    
                # Robust case-insensitive lookup
                from sqlalchemy import or_, func
                target_team = Team.query.filter(
                    Team.event_id == event_id,
                    or_(
                        func.lower(Team.team_id_code) == t_code_or_name.lower(),
                        func.lower(Team.team_name) == t_code_or_name.lower()
                    )
                ).first()
                    
                if not target_team:
                    # Check if the team exists in ANY event to provide a better error
                    other_event_team = Team.query.filter(
                        or_(
                            func.lower(Team.team_id_code) == t_code_or_name.lower(),
                            func.lower(Team.team_name) == t_code_or_name.lower()
                        )
                    ).first()
                    
                    if other_event_team:
                        flash(f'The team "{t_code_or_name}" exists but is registered for a different event: "{other_event_team.event.name}".', 'warning')
                    else:
                        # Log failed attempt for debugging
                        with open('instance/join_debug.log', 'a') as f:
                            import os
                            if not os.path.exists('instance'): os.makedirs('instance')
                            f.write(f"{datetime.now()}: Join Failed. Event {event_id}, User {current_user.id}, Attempt: '{t_code_or_name}'\n")
                        flash(f'Team "{t_code_or_name}" not found. Please verify the Code or Name and ensure it belongs to this event.', 'danger')
                    return redirect(request.url)
                
                # Check current member count
                memo_count = TeamMember.query.filter_by(team_id=target_team.id).count()
                if memo_count >= target_team.declared_size:
                    flash(f'Team "{target_team.team_name}" is already full ({target_team.declared_size} members).', 'danger')
                    return redirect(request.url)
                
                team_id = target_team.id
                # Check if already a member through other means
                existing_member = TeamMember.query.filter_by(team_id=team_id, user_id=current_user.id).first()
                if not existing_member:
                    new_mem = TeamMember(team_id=team_id, user_id=current_user.id, role='member')
                    db.session.add(new_mem)
                db.session.flush()

        # Collect custom form data
        responses = {}
        target_team = None
        if team_id:
            target_team = Team.query.get(team_id)

        skip_form = (ptype == 'team' and request.form.get('team_action') == 'join')
        
        if skip_form and target_team:
            # Inherit Lead's responses
            lead_resp = EventResponse.query.filter_by(event_id=event_id, student_id=target_team.leader_id).first()
            if lead_resp:
                responses = json.loads(lead_resp.response_json)
        else:
            schema = json.loads(form_obj.schema_json) if form_obj and form_obj.schema_json else []
            for field in schema:
                f_lower = field['label'].lower()
                if f_lower in ['team name', 'teamname', 'name of team', 'name of the team'] and event.participation_type in ['team', 'both'] and ptype == 'team':
                    responses[field['label']] = target_team.team_name if (request.form.get('team_action') == 'join') else t_name
                else:
                    val = request.form.get(f"field_{field['label']}")
                    if field.get('required') and not val:
                        flash(f"{field['label']} is required", 'danger')
                        return redirect(request.url)
                    responses[field['label']] = val

        # Helper to generate tickets
        def create_ticket_record(stu_id, t_id):
            import qrcode, io, base64
            student = User.query.get(stu_id)
            
            # Ticket ID generation logic - Use Team Lead's details if in a team
            id_source_student = student
            if t_id:
                t_obj = Team.query.get(t_id)
                if t_obj:
                    lead_stu = User.query.get(t_obj.leader_id)
                    if lead_stu:
                        id_source_student = lead_stu

            prefix = id_source_student.roll_no[:2] if id_source_student.roll_no and len(id_source_student.roll_no) >= 2 else "00"
            dept_code = get_dept_code(id_source_student.department)
            
            # Ensure ticket_id is globally unique
            serial = EventResponse.query.filter_by(event_id=event_id).count() + 1
            while True:
                candidate_id = f"{prefix}{dept_code}{str(serial).zfill(4)}"
                if not EventResponse.query.filter_by(ticket_id=candidate_id).first():
                    ticket_id = candidate_id
                    break
                serial += 1
            
            # QR Data: Basic details + Team list
            qr_text = f"Ticket ID: {ticket_id}\nName: {student.get_full_name()}\nRoll No: {student.roll_no}\nEvent: {event.name}\nDate: {event.start_date.strftime('%d %b %Y') if event.start_date else 'TBA'}"
            
            if t_id:
                from models import TeamMember
                all_mems = TeamMember.query.filter_by(team_id=t_id).all()
                mem_list = "\nTeam Members:\n" + "\n".join([f"- {m.user.get_full_name()} ({m.user.roll_no})" for m in all_mems])
                qr_text += mem_list

            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(qr_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            qr_base64 = base64.b64encode(buffered.getvalue()).decode()

            r = EventResponse(
                event_id=event_id,
                student_id=stu_id,
                team_id=t_id,
                response_json=json.dumps(responses),
                ticket_id=ticket_id,
                qr_code_data=qr_base64
            )
            db.session.add(r)
            db.session.flush() # To get ID for leader_response
            return r

        leader_response = create_ticket_record(current_user.id, team_id)
        
        for ex_mem in extra_members_found:
            create_ticket_record(ex_mem.id, team_id)

        db.session.commit()
        
        flash('Registration successful!', 'success')
        return redirect(url_for('club_portal.registration_ticket', response_id=leader_response.id))

    schema = json.loads(form_obj.schema_json) if form_obj and form_obj.schema_json else []
    return render_template('club/apply_event.html', event=event, schema=schema, now=datetime.utcnow())


@bp.route('/my-registrations/<int:response_id>')
@login_required
def registration_ticket(response_id):
    response = EventResponse.query.get_or_404(response_id)
    if response.student_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))
    
    team = Team.query.get(response.team_id) if response.team_id else None
    
    # Ticket Status Logic
    ticket_status = 'VALID'
    status_msg = ''
    
    if team:
        from models import TeamMember
        members_count = TeamMember.query.filter_by(team_id=team.id).count()
        event = response.event
        
        if members_count < event.team_size_min:
            now = datetime.utcnow() + timedelta(hours=5, minutes=30)
            deadline = event.registration_deadline or event.start_date or datetime.combine(event.date, datetime.min.time())
            
            if now > deadline:
                if event.participation_type in ['solo', 'both'] and members_count == 1:
                    ticket_status = 'ACCEPTED_SOLO'
                    status_msg = 'Deadline passed: Automatically accepted as Solo participant.'
                else:
                    ticket_status = 'REJECTED'
                    status_msg = f'Registration Rejected: Minimum team size of {event.team_size_min} was not met by the deadline.'
            else:
                ticket_status = 'PENDING'
                status_msg = f'Pending Members: Your team needs {event.team_size_min - members_count} more member(s) to validate this ticket.'

    return render_template('club/ticket.html', response=response, team=team, ticket_status=ticket_status, status_msg=status_msg)

@bp.route('/events/<int:event_id>/scanner')
def scanner(event_id):
    club_id = session.get('club_id')
    if not club_id: return redirect(url_for('club_portal.login'))
    
    event = Event.query.get_or_404(event_id)
    if event.club_id != club_id: return "Unauthorized", 403
    
    return render_template('club/scanner.html', event=event)

@bp.route('/mark-attendance', methods=['POST'])
def mark_attendance():
    club_id = session.get('club_id')
    if not club_id: return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    data = request.get_json()
    raw_scan = data.get('ticket_id', '')
    event_id = data.get('event_id')
    session_type = data.get('session_type', 'Default')
    
    if not event_id:
        return jsonify({'success': False, 'message': 'Event ID missing'}), 400

    event = Event.query.get(event_id)
    if not event or event.club_id != club_id:
        return jsonify({'success': False, 'message': 'Invalid Event'}), 404

    # Date check: Only allow scanning on the day of the event
    today = datetime.now().date()
    event_start = event.start_date.date() if event.start_date else event.date
    event_end = event.end_date.date() if event.end_date else event_start
    
    if today < event_start or today > event_end:
        return jsonify({
            'success': False, 
            'message': f'Attendance scanning is only allowed on the day of the event ({event_start.strftime("%d %b %Y")})'
        }), 403

    # Extract Ticket ID if it's the full multi-line QR text
    ticket_id = raw_scan
    if "Ticket ID:" in raw_scan:
        try:
            # Format is "Ticket ID: ABCD0001\n..."
            ticket_id = raw_scan.split('\n')[0].replace('Ticket ID:', '').strip()
        except:
            ticket_id = raw_scan.strip()
    else:
        ticket_id = raw_scan.strip()
    
    response = EventResponse.query.filter_by(ticket_id=ticket_id, event_id=event_id).first()
    if not response:
        # Check if they exist for a DIFFERENT event of this club
        any_response = EventResponse.query.filter_by(ticket_id=ticket_id).first()
        if any_response:
             return jsonify({'success': False, 'message': f'Student is registered for "{any_response.event.name}", not this event.'}), 400
        return jsonify({'success': False, 'message': 'Invalid Ticket or Student not registered for this event.'}), 404
        
    # Check if this student belongs to an event of this club
    if response.event.club_id != club_id:
        return jsonify({'success': False, 'message': 'Ticket does not belong to this club'}), 403
        
    # Validate Team Completeness
    if response.team_id:
        from models import TeamMember
        members_count = TeamMember.query.filter_by(team_id=response.team_id).count()
        if members_count < event.team_size_min:
            now = datetime.utcnow() + timedelta(hours=5, minutes=30)
            deadline = event.registration_deadline or event.start_date or datetime.combine(event.date, datetime.min.time())
            
            if now > deadline:
                if event.participation_type in ['solo', 'both'] and members_count == 1:
                    # Validated as fallback solo
                    pass
                else:
                    return jsonify({'success': False, 'message': f'Registration Rejected: Minimum team size of {event.team_size_min} was not met by the deadline.'}), 400
            else:
                return jsonify({'success': False, 'message': f'Team is incomplete. Requires {event.team_size_min - members_count} more member(s).'}), 400
        
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
    
    raw_responses = EventResponse.query.filter_by(event_id=event_id).all()
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    responses = filter_accepted_responses(event, raw_responses, now)
    
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
    
    raw_responses = EventResponse.query.filter_by(event_id=event_id).all()
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    responses = filter_accepted_responses(event, raw_responses, now)
    
    teams = {t.id: t for t in Team.query.filter_by(event_id=event_id).all()}
    
    grouped_teams = {}
    solo_responses = []
    
    # Pre-parse JSON for template and group
    for r in responses:
        r.custom_data = json.loads(r.response_json) if r.response_json else {}
        
        team_name = None
        is_lead = False
        if r.team_id and r.team_id in teams:
            t_obj = teams[r.team_id]
            team_name = t_obj.team_name
            if t_obj.leader_id == r.student_id:
                is_lead = True
        else:
            for k, v in r.custom_data.items():
                if k.lower() in ['team name', 'teamname', 'name of team', 'name of the team']:
                    team_name = v
                    break
        
        r.is_leader = is_lead
                    
        if team_name:
            if team_name not in grouped_teams:
                grouped_teams[team_name] = []
            grouped_teams[team_name].append(r)
        else:
            solo_responses.append(r)
        
    return render_template('club/participants.html', event=event, responses=responses, grouped_teams=grouped_teams, solo_responses=solo_responses)
