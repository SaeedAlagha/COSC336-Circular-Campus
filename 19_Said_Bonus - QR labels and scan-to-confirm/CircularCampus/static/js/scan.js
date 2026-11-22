/* Bonus: QR scanner. Uses the browser's BarcodeDetector when present, otherwise the bundled jsQR
   library (Apache-2.0). The camera only works on https:// or http://localhost — typing the printed
   code always works as a fallback. Nothing leaves the device: frames are decoded locally. */
(function () {
  "use strict";
  document.querySelectorAll("[data-scanner]").forEach(function (box) {
    var video = box.querySelector("video"), canvas = box.querySelector("canvas"), start = box.querySelector("[data-scan-start]");
    var status = box.querySelector("[data-scan-status]"), target = document.querySelector(box.dataset.target);
    var stream = null, detector = null, timer = null;
    function say(text, ok) { status.textContent = text; status.classList.toggle("ok", !!ok); }
    function stop() {
      if (timer) cancelAnimationFrame(timer);
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null; box.classList.remove("live"); start.hidden = false;
    }
    function found(text) {
      stop();
      var tag = text.replace(/\/+$/, "").split("/").pop().trim().toUpperCase();
      if (target) { target.value = text.trim(); target.dispatchEvent(new Event("input", { bubbles: true })); }
      if (box.dataset.expect && tag !== box.dataset.expect.toUpperCase()) {
        say("Scanned " + tag + " — this is not " + box.dataset.expect + ". Check the item.", false); return;
      }
      say("Scanned " + tag + " ✓", true);
      if (box.dataset.mode === "go" && target && target.form) target.form.submit();
    }
    function tick() {
      if (!stream) return;
      if (video.readyState >= 2) {
        if (detector) {
          detector.detect(video).then(function (codes) { if (codes.length) found(codes[0].rawValue); else timer = requestAnimationFrame(tick); })
            .catch(function () { timer = requestAnimationFrame(tick); });
          return;
        }
        if (window.jsQR) {
          var w = video.videoWidth, h = video.videoHeight;
          canvas.width = w; canvas.height = h;
          var ctx = canvas.getContext("2d", { willReadFrequently: true });
          ctx.drawImage(video, 0, 0, w, h);
          var code = window.jsQR(ctx.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: "dontInvert" });
          if (code && code.data) { found(code.data); return; }
        }
      }
      timer = requestAnimationFrame(tick);
    }
    start.addEventListener("click", function () {
      if (!navigator.mediaDevices || !window.isSecureContext) {
        say("The camera needs https:// or localhost. Type the code printed under the label instead.", false); return;
      }
      if ("BarcodeDetector" in window) { try { detector = new window.BarcodeDetector({ formats: ["qr_code"] }); } catch (e) { detector = null; } }
      navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false }).then(function (s) {
        stream = s; video.srcObject = s; video.play(); box.classList.add("live"); start.hidden = true;
        say("Point the camera at the QR label…", false); tick();
      }).catch(function () { say("Camera not available or permission denied. Type the printed code instead.", false); });
    });
    var cancel = box.querySelector("[data-scan-stop]");
    cancel && cancel.addEventListener("click", function () { stop(); say("Camera stopped.", false); });
  });
})();
