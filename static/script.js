// ELEMENTS
const textarea = document.getElementById("messageInput");
const chatForm = document.getElementById("chatForm");


// AUTO RESIZE TEXTAREA ANIMATION
textarea.addEventListener("input", () => {
  textarea.style.height = "auto";
  textarea.style.height = textarea.scrollHeight + "px";
});

// ENTER KEY SEND
textarea.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

const chatMessages = document.getElementById("chatMessages");
const welcomeScreen = document.getElementById("welcomeScreen");
const historyToggle = document.getElementById("historyToggle");
const recentHistory = document.getElementById("recentHistory");

if (historyToggle && recentHistory) {
  historyToggle.addEventListener("click", () => {
    recentHistory.classList.toggle("open");
  });
}

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = textarea.value.trim();
  if (!message) return;

  if (welcomeScreen) {
    welcomeScreen.style.display = "none";
  }

  const userBubble = document.createElement("div");
  userBubble.className = "message user-message";
  userBubble.textContent = message;
  chatMessages.appendChild(userBubble);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  // Show loading indicator
  const loadingBubble = document.createElement("div");
  loadingBubble.className = "message bot-message";
  loadingBubble.textContent = "thinking...";
  loadingBubble.id = "loadingBubble";
  chatMessages.appendChild(loadingBubble);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  // Disable send button while processing
  const sendBtn = chatForm.querySelector(".send-btn");
  sendBtn.disabled = true;

  const response = await fetch(chatForm.action, {
    method: "POST",
    body: new FormData(chatForm),
  });

  let data;
  try {
    data = await response.json();
  } catch (err) {
    data = { reply: "Unable to get a response from the server." };
  }

  // Replace loading bubble with actual response
  const loadingElement = document.getElementById("loadingBubble");
  if (loadingElement) {
    loadingElement.textContent = data.reply || "No response received.";
    loadingElement.id = ""; // Remove ID so new messages don't interfere
  }

  chatMessages.scrollTop = chatMessages.scrollHeight;

  textarea.value = "";
  textarea.style.height = "auto";

  // Re-enable send button
  sendBtn.disabled = false;
  textarea.focus();
});



// SPEECH RECOGNIZATION
const SpeechRecognition =
    window.SpeechRecognition ||
    window.webkitSpeechRecognition;

const recognition = new SpeechRecognition();

recognition.lang = "en-IN";
recognition.continuous = false;
recognition.interimResults = false;

document.getElementById("micBtn").addEventListener("click", () => {
    recognition.start();
});

recognition.onresult = (event) => {
    const text = event.results[0][0].transcript;
    textarea.value = text;
    console.log("Recognized:", text);
};

recognition.onerror = (event) => {
    console.log("Speech Error:", event.error);
};

document.getElementById("chatForm").addEventListener("submit", function () {
    document.getElementById("messageInput").value = "";
});