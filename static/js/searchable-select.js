/**
 * SearchableSelect - Transforms standard HTML <select> elements into sleek, searchable comboboxes.
 */
function bindOptionActivation(item, onSelect) {
    item.addEventListener('mousedown', (event) => {
        // Keep focus on the combobox until the pointer sequence completes.
        // Closing on mousedown lets the subsequent mouseup/click fall through
        // to controls underneath the dropdown.
        event.preventDefault();
        event.stopPropagation();
    });
    item.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        onSelect();
    });
}

function moveActiveIndex(currentIndex, direction, optionCount) {
    if (!optionCount) return -1;
    if (direction > 0) return currentIndex < optionCount - 1 ? currentIndex + 1 : 0;
    return currentIndex > 0 ? currentIndex - 1 : optionCount - 1;
}

class SearchableSelect {
    constructor(selectEl) {
        if (!selectEl || selectEl.dataset.searchableInit) return;
        this.selectEl = selectEl;
        this.selectEl.dataset.searchableInit = "true";
        this.options = [];
        this.renderedOptions = [];
        this.activeIndex = -1;
        this.isOpen = false;
        this.init();
    }

    init() {
        this.selectEl.style.display = 'none';
        this.wasRequired = this.selectEl.required;
        this.selectEl.required = false;
        this.selectEl.tabIndex = -1;
        this.selectEl.setAttribute('aria-hidden', 'true');

        this.container = document.createElement('div');
        this.container.className = 'searchable-select';

        this.control = document.createElement('div');
        this.control.className = 'searchable-select__control';

        this.input = document.createElement('input');
        this.input.type = 'text';
        this.input.className = 'searchable-select__input';
        this.input.placeholder = 'Search or select...';
        this.input.autocomplete = 'off';
        this.input.required = this.wasRequired;
        this.input.disabled = this.selectEl.disabled;
        this.input.setAttribute('role', 'combobox');
        this.input.setAttribute('aria-autocomplete', 'list');
        this.input.setAttribute('aria-expanded', 'false');

        const baseId = this.selectEl.id || `searchable-select-${SearchableSelect.nextId++}`;
        if (!this.selectEl.id) this.selectEl.id = baseId;
        this.input.id = `${baseId}-combobox`;
        this.dropdownId = `${baseId}-listbox`;
        this.input.setAttribute('aria-controls', this.dropdownId);
        const label = document.querySelector(`label[for="${CSS.escape(baseId)}"]`);
        if (label) {
            if (!label.id) label.id = `${baseId}-label`;
            label.htmlFor = this.input.id;
            this.input.setAttribute('aria-labelledby', label.id);
        } else {
            this.input.setAttribute(
                'aria-label',
                this.selectEl.getAttribute('aria-label') || this.selectEl.name || 'Select an option'
            );
        }

        this.arrow = document.createElement('span');
        this.arrow.className = 'searchable-select__arrow';
        this.arrow.textContent = '▼';
        this.arrow.setAttribute('aria-hidden', 'true');

        this.control.appendChild(this.input);
        this.control.appendChild(this.arrow);

        this.dropdown = document.createElement('div');
        this.dropdown.className = 'searchable-select__dropdown';
        this.dropdown.id = this.dropdownId;
        this.dropdown.setAttribute('role', 'listbox');

        this.container.appendChild(this.control);
        this.container.appendChild(this.dropdown);

        this.selectEl.parentNode.insertBefore(this.container, this.selectEl.nextSibling);

        this.bindEvents();
        this.refresh();
        this.syncValidity();
    }

    refresh() {
        this.options = Array.from(this.selectEl.options).map((opt, idx) => ({
            value: opt.value,
            text: opt.text,
            code: opt.dataset.roomCode || opt.dataset.room || '',
            element: opt,
            index: idx
        }));
        this.updateInputValue();
    }

    updateInputValue() {
        const selectedOpt = this.options.find(o => o.value === this.selectEl.value);
        if (selectedOpt && selectedOpt.value !== '') {
            this.input.value = selectedOpt.text;
        } else {
            this.input.value = '';
            this.input.placeholder = this.selectEl.options[0]?.text || 'Search or select...';
        }
        this.syncValidity();
    }

    syncValidity() {
        const missingSelection = this.wasRequired && !this.selectEl.value;
        this.input.setCustomValidity(missingSelection ? 'Please select an option.' : '');
    }

    renderDropdown(filterText = '') {
        this.dropdown.innerHTML = '';
        const query = filterText.trim().toLowerCase();

        const matches = this.options.filter(opt => {
            if (!opt.value) return false;
            if (!query) return true;
            return opt.text.toLowerCase().includes(query) || opt.code.toLowerCase().includes(query);
        });
        this.renderedOptions = matches;
        this.activeIndex = -1;
        this.input.removeAttribute('aria-activedescendant');

        if (matches.length === 0) {
            const noResults = document.createElement('div');
            noResults.className = 'searchable-select__no-results';
            noResults.textContent = 'No matching options found';
            noResults.setAttribute('role', 'status');
            this.dropdown.appendChild(noResults);
            return;
        }

        matches.forEach((opt, index) => {
            const item = document.createElement('div');
            item.className = 'searchable-select__option';
            item.id = `${this.dropdownId}-option-${index}`;
            item.setAttribute('role', 'option');
            item.setAttribute('aria-selected', String(opt.value === this.selectEl.value));
            if (opt.value === this.selectEl.value) {
                item.classList.add('is-selected');
            }
            item.textContent = opt.text;
            bindOptionActivation(item, () => this.selectOption(opt));
            this.dropdown.appendChild(item);
        });
    }

    selectOption(opt) {
        this.selectEl.value = opt.value;
        this.input.value = opt.text;
        this.close();
        this.syncValidity();
        this.selectEl.dispatchEvent(new Event('change', { bubbles: true }));
        this.selectEl.dispatchEvent(new Event('input', { bubbles: true }));
    }

    open() {
        this.refresh();
        const currentText = this.getSelectedText();
        this.renderDropdown(this.input.value === currentText ? '' : this.input.value);
        this.container.classList.add('is-open');
        this.isOpen = true;
        this.input.setAttribute('aria-expanded', 'true');
    }

    close() {
        this.container.classList.remove('is-open');
        this.isOpen = false;
        this.activeIndex = -1;
        this.input.setAttribute('aria-expanded', 'false');
        this.input.removeAttribute('aria-activedescendant');
        this.updateInputValue();
    }

    moveActive(direction) {
        if (!this.isOpen) this.open();
        this.activeIndex = moveActiveIndex(
            this.activeIndex,
            direction,
            this.renderedOptions.length
        );
        const items = Array.from(this.dropdown.querySelectorAll('[role="option"]'));
        items.forEach((item, index) => {
            const active = index === this.activeIndex;
            item.classList.toggle('is-highlighted', active);
            if (active) {
                this.input.setAttribute('aria-activedescendant', item.id);
                item.scrollIntoView({ block: 'nearest' });
            }
        });
    }

    getSelectedText() {
        const selectedOpt = this.options.find(o => o.value === this.selectEl.value);
        return selectedOpt ? selectedOpt.text : '';
    }

    bindEvents() {
        this.input.addEventListener('focus', () => {
            this.open();
        });

        this.input.addEventListener('click', () => {
            if (!this.isOpen) {
                this.open();
            }
        });

        this.input.addEventListener('input', () => {
            if (!this.isOpen) {
                this.open();
            }
            if (this.input.value !== this.getSelectedText()) {
                this.selectEl.value = '';
                this.syncValidity();
            }
            this.renderDropdown(this.input.value);
        });

        this.input.addEventListener('keydown', (event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                this.moveActive(event.key === 'ArrowDown' ? 1 : -1);
                return;
            }
            if (event.key === 'Enter' && this.isOpen && this.activeIndex >= 0) {
                event.preventDefault();
                this.selectOption(this.renderedOptions[this.activeIndex]);
                return;
            }
            if (event.key === 'Escape' && this.isOpen) {
                event.preventDefault();
                this.close();
            }
        });

        this.input.addEventListener('blur', () => {
            setTimeout(() => this.close(), 150);
        });

        this.selectEl.addEventListener('change', () => {
            this.updateInputValue();
        });

        this.selectEl.form?.addEventListener('reset', () => {
            setTimeout(() => this.updateInputValue(), 0);
        });
    }
}

SearchableSelect.nextId = 1;

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('select.searchable-select').forEach(select => {
            new SearchableSelect(select);
        });
    });
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { bindOptionActivation, moveActiveIndex };
}
