// ═══════════════════════════════════════════════════════════════════════════
// report.js — User Reporting Module logic
// ═══════════════════════════════════════════════════════════════════════════

let stream = null;
let capturedBlob = null;
let currentCoords = null;
let useFacingMode = 'environment';

const video = document.getElementById('cameraStream');
const canvas = document.createElement('canvas');
const preview = document.getElementById('capturedPreview');
const captureBtn = document.getElementById('captureBtn');
const retakeBtn = document.getElementById('retakeBtn');
const submitBtn = document.getElementById('submitBtn');
const gpsStatus = document.getElementById('gpsStatus');
const fileInput = document.getElementById('fileInput');

/* ── Camera Logic ────────────────────────────────────────────────────────── */
async function startCamera() {
  try {
    if (stream) {
      stream.getTracks().forEach(track => track.stop());
    }
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: useFacingMode },
      audio: false
    });
    video.srcObject = stream;
    video.style.display = 'block';
    preview.style.display = 'none';
  } catch (err) {
    console.error("Camera error:", err);
    showToast("Could not access camera. Please use file upload.", "error");
  }
}

function captureImage() {
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  
  canvas.toBlob((blob) => {
    capturedBlob = blob;
    const url = URL.createObjectURL(blob);
    preview.src = url;
    preview.style.display = 'block';
    video.style.display = 'none';
    
    captureBtn.style.display = 'none';
    retakeBtn.style.display = 'flex';
    checkSubmitReady();
  }, 'image/jpeg', 0.8);
}

function retake() {
  capturedBlob = null;
  preview.style.display = 'none';
  video.style.display = 'block';
  captureBtn.style.display = 'flex';
  retakeBtn.style.display = 'none';
  checkSubmitReady();
}

/* ── GPS Logic ───────────────────────────────────────────────────────────── */
function initGPS() {
  if (!navigator.geolocation) {
    gpsStatus.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> GPS not supported';
    gpsStatus.className = 'gps-status searching';
    return;
  }

  navigator.geolocation.watchPosition(
    (pos) => {
      currentCoords = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude
      };
      document.getElementById('latInput').value = currentCoords.lat.toFixed(6);
      document.getElementById('lonInput').value = currentCoords.lon.toFixed(6);
      
      gpsStatus.innerHTML = '<i class="fa-solid fa-circle-check"></i> GPS Location Ready';
      gpsStatus.className = 'gps-status ready';
      checkSubmitReady();
    },
    (err) => {
      console.warn("GPS Error:", err);
      gpsStatus.innerHTML = '<i class="fa-solid fa-circle-exclamation"></i> GPS Error: ' + err.message;
      gpsStatus.className = 'gps-status searching';
    },
    { enableHighAccuracy: true }
  );
}

/* ── Submission Logic ────────────────────────────────────────────────────── */
function checkSubmitReady() {
  submitBtn.disabled = !(capturedBlob && currentCoords);
}

async function handleSubmit() {
  if (!capturedBlob || !currentCoords) return;

  submitBtn.disabled = true;
  submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Submitting...';

  const formData = new FormData();
  formData.append('media', capturedBlob, 'capture.jpg');
  formData.append('lat', currentCoords.lat);
  formData.append('lon', currentCoords.lon);
  formData.append('description', document.getElementById('descInput').value);

  try {
    const res = await fetch('/api/report', {
      method: 'POST',
      body: formData
    });
    const data = await res.json();

    if (res.ok) {
      showToast("Report submitted successfully!", "success");
      setTimeout(() => window.location.href = '/', 2000);
    } else {
      showToast(data.error || "Submission failed", "error");
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Submit Report';
    }
  } catch (err) {
    showToast("Network error occurred", "error");
    submitBtn.disabled = false;
    submitBtn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Submit Report';
  }
}

/* ── File Upload Fallback ───────────────────────────────────────────────── */
fileInput.onchange = (e) => {
  const file = e.target.files[0];
  if (!file) return;

  capturedBlob = file;
  if (file.type.startsWith('image/')) {
    preview.src = URL.createObjectURL(file);
    preview.style.display = 'block';
    video.style.display = 'none';
  } else {
    // For video, we just show a generic icon or status
    preview.style.display = 'none';
    showToast("Video file selected: " + file.name, "info");
  }
  
  captureBtn.style.display = 'none';
  retakeBtn.style.display = 'flex';
  checkSubmitReady();
};

/* ── Boot ────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  startCamera();
  initGPS();

  captureBtn.onclick = captureImage;
  retakeBtn.onclick = retake;
  submitBtn.onclick = handleSubmit;
  
  document.getElementById('switchCameraBtn').onclick = () => {
    useFacingMode = useFacingMode === 'user' ? 'environment' : 'user';
    startCamera();
  };

  document.getElementById('uploadDrop').onclick = () => fileInput.click();
});
