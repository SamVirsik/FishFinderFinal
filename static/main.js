


//   document.addEventListener('mousemove', (e) => {
//     if (!isDragging) return;
//     // Constrain within the parent container if needed
//     const parentRect = toolbar.parentElement.getBoundingClientRect();
//     let newLeft = e.clientX - offsetX;
//     let newTop = e.clientY - offsetY;
//     // Optional: prevent moving outside parent boundaries
//     console.log(toolbar.offsetWidth, toolbar.offsetHeight);
//     newLeft = Math.max(10, Math.min(newLeft, parentRect.width - toolbar.offsetWidth-10));
//     newTop = Math.max(10, Math.min(newTop, parentRect.height - toolbar.offsetHeight-10));
//     toolbar.style.left = newLeft + 'px';
//     toolbar.style.top = newTop + 'px';
//   });

  // Stop dragging
//   document.addEventListener('mouseup', () => {
//     isDragging = false;
//   });



// Navigation logic
const loginLink = document.getElementById("login-link");
const loginModal = document.getElementById("login-modal");
const loginClose = document.getElementById("login-close");

loginLink.addEventListener("click", (e) => {
    e.preventDefault();
    loginModal.style.display = "block";
});

loginClose.addEventListener("click", () => {
    loginModal.style.display = "none";
});

window.onclick = (event) => {
    if (event.target === loginModal) {
        loginModal.style.display = "none";
    }
};