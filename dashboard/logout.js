const logoutButton = document.createElement('button');
logoutButton.className = 'logout-button';
logoutButton.type = 'button';
logoutButton.textContent = 'Logout';
logoutButton.addEventListener('click', async () => {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
  window.location.href = '/login';
});

window.addEventListener('load', () => {
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) {
    const actions = document.createElement('div');
    actions.className = 'sidebar-logout';
    actions.appendChild(logoutButton);
    sidebar.appendChild(actions);
  }
});
