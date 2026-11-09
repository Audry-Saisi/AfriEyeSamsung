// Plays the role of the phone network: it sends each answer to AfriEye the same way Africa's Talking does.
(function () {
  let answers = [], open = false, session = "";
  const $ = (id) => document.getElementById(id);
  async function send() {
    let phone = $("phone").value.replace(/[\s\-()]/g, ""); if (/^0[17]\d{8}$/.test(phone)) phone = "+254" + phone.slice(1);
    const body = new URLSearchParams({ sessionId: session, serviceCode: "*384*33#", phoneNumber: phone, text: answers.join("*") });
    try {
      const r = await fetch("/api/ussd/demo", { method: "POST", body });
      const text = await r.text();
      if (!r.ok) { $("screen").textContent = "This USSD menu is turned off on this server."; open = false; return; }
      open = text.startsWith("CON "); $("screen").textContent = text.slice(4) + (open ? "" : "\n\n(Session ended)");
      if (open) $("reply").focus();
    } catch (e) { $("screen").textContent = "No connection to the AfriEye server."; open = false; }
  }
  $("dial").onclick = () => { answers = []; session = "s" + Date.now(); open = true; send(); };
  $("form").onsubmit = (e) => { e.preventDefault(); if (!open) return; const v = $("reply").value.trim(); if (!v) return; answers.push(v); $("reply").value = ""; send(); };
  $("end").onclick = () => { open = false; answers = []; $("screen").textContent = "Session ended."; };
})();
