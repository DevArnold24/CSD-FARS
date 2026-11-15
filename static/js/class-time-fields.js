(() => {
    const panel = document.querySelector('[data-class-times]');
    if (!panel) return;
    const summary = panel.querySelector('[data-time-summary]');
    const pad = value => String(value).padStart(2, '0');
    const fields = Array.from(panel.querySelectorAll('[data-time-field]')).map(group => {
        const input = group.querySelector('input[type="time"]');
        const controls = Object.fromEntries(Array.from(group.querySelectorAll('select[data-part]'))
            .map(select => [select.dataset.part, select]));
        const error = group.querySelector('.class-time-error');
        const ending = input.name === 'end_time';
        const field = {group, input, controls, error, ending, seconds: false};
        const match = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(input.value);
        if (match) {
            const hour = Number(match[1]);
            controls.hour.value = String(hour % 12 || 12);
            controls.minute.value = match[2];
            controls.period.value = hour >= 12 ? 'PM' : 'AM';
            controls.second.value = match[3] || '00';
            field.seconds = controls.second.value !== '00';
        }
        group.querySelector('[data-time-part="second"]').hidden = !field.seconds;
        group.querySelector('[data-seconds-help]').hidden = !field.seconds;
        for (const [part, select] of Object.entries(controls)) {
            select.disabled = part === 'second' && !field.seconds;
            select.required = !select.disabled;
            select.addEventListener('change', () => update());
            select.addEventListener('invalid', () => {
                error.textContent = select.validationMessage;
                error.hidden = false;
                select.setAttribute('aria-invalid', 'true');
            });
        }
        group.querySelector('[data-time-choices]').hidden = false;
        group.querySelector('[data-time-fallback]').hidden = true;
        input.required = false;
        return field;
    });

    function sync(field) {
        const {controls, input, error, ending} = field;
        error.hidden = true;
        let complete = true;
        for (const [part, select] of Object.entries(controls)) {
            select.removeAttribute('aria-invalid');
            const label = {hour: 'an hour', minute: 'minutes', period: 'AM or PM', second: 'seconds'}[part];
            const missing = !select.disabled && !select.value;
            select.setCustomValidity(missing ? `Choose ${label} for the class ${ending ? 'end' : 'start'} time.` : '');
            if (missing) complete = false;
        }
        if (!complete) { input.value = ''; return null; }
        const hour = Number(controls.hour.value) % 12 + (controls.period.value === 'PM' ? 12 : 0);
        const second = field.seconds ? Number(controls.second.value) : 0;
        input.value = `${pad(hour)}:${controls.minute.value}:${pad(second)}`;
        return hour * 3600 + Number(controls.minute.value) * 60 + second;
    }

    function display(field) {
        const c = field.controls;
        return `${c.hour.value}:${c.minute.value}${field.seconds ? ':' + c.second.value : ''} ${c.period.value}`;
    }

    function update() {
        const [start, end] = fields.map(sync);
        summary.hidden = true;
        if (start === null || end === null) return;
        if (end <= start) {
            const message = 'Choose an end time after the start time on the same day. Overnight classes are not supported.';
            fields[1].controls.period.setCustomValidity(message);
            fields[1].controls.period.setAttribute('aria-invalid', 'true');
            fields[1].error.textContent = message;
            fields[1].error.hidden = false;
            return;
        }
        const duration = end - start;
        const units = [[Math.floor(duration / 3600), 'hour'], [Math.floor(duration % 3600 / 60), 'minute'], [duration % 60, 'second']]
            .filter(([amount]) => amount).map(([amount, name]) => `${amount} ${name}${amount === 1 ? '' : 's'}`);
        summary.textContent = `Class runs from ${display(fields[0])} to ${display(fields[1])} · ${units.join(' ')}.`;
        summary.hidden = false;
    }
    update();
})();
