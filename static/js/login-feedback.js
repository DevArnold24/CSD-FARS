/* A rejected sign-in should lead to its explanation, not back to Username. */
(() => {
    const error = document.querySelector('[data-login-error]');
    if (!error) return;
    error.focus({preventScroll: true});
    error.scrollIntoView({behavior: 'instant', block: 'center'});
})();
