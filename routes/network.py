"""Network observations for Dean and IT Support; configuration uses Setup Center."""
from flask import current_app, jsonify, render_template, request

from core.utils import login_required
from database.db_manager import managed_transaction
from routes.blueprints import diagnostics_bp
from routes.setup import _public_url, _runtime_mode
from security.rbac import permission_required
from services.camera_health import describe_stored_camera_health


@diagnostics_bp.after_request
def network_private_response(response):
    if request.path in ('/system/network', '/api/diagnostics/network'):
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@diagnostics_bp.get('/system/network')
@login_required
@permission_required('view_diagnostics')
def network_index():
    return render_template('diagnostics/network.html')


@diagnostics_bp.get('/api/diagnostics/network')
@login_required
@permission_required('view_diagnostics')
def network_status():
    data = current_app.extensions['csd_network_monitor'].snapshot()
    mode = _runtime_mode()
    data['server'] = {'mode': mode, 'public_url': _public_url() if mode == 'lan' else None}
    data['clients'] = current_app.extensions['csd_recent_clients'].snapshot()
    try:
        with managed_transaction(commit=False) as conn:
            cameras = [dict(row) for row in conn.execute(
                'SELECT cd.id, cd.name, cd.camera_type, cd.is_active, cd.lifecycle_status, '
                'cd.last_test_status, cd.last_health_check_at, r.room_code '
                'FROM camera_devices cd LEFT JOIN rooms r ON r.id=cd.room_id ORDER BY cd.id LIMIT 129'
            )]
        data['cameras'] = {'available': True, 'truncated': len(cameras) > 128, 'items': [
            {'name': camera['name'], 'room': camera['room_code'], 'enabled': bool(camera['is_active']),
             'type': 'Local camera' if camera['camera_type'] == 'local' else 'Network camera',
             'check': describe_stored_camera_health(camera)} for camera in cameras[:128]
        ]}
    except Exception:
        data['cameras'] = {'available': False, 'items': []}
    return jsonify(data)
