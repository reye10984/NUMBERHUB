(function () {
  var btn = document.getElementById("refresh");
  if (!btn) return;
  btn.addEventListener("click", function () {
    var status = document.getElementById("status"), list = document.getElementById("msgs");
    status.textContent = "Refreshing...";
    fetch("/api/messages", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (rows) {
        list.textContent = "";
        if (!rows.length) { var e = document.createElement("li"); e.className = "empty"; e.textContent = "No messages yet."; list.appendChild(e); }
        rows.forEach(function (m) {
          var li = document.createElement("li");
          var b = document.createElement("b"); b.textContent = m.sender; li.appendChild(b);
          li.appendChild(document.createTextNode(" to " + m.number + " "));
          var s = document.createElement("small"); s.textContent = m.received_at; li.appendChild(s);
          var d = document.createElement("div"); d.textContent = m.body; li.appendChild(d);
          list.appendChild(li);
        });
        status.textContent = "Updated " + new Date().toLocaleTimeString();
      })
      .catch(function (err) { status.textContent = "Could not refresh: " + err.message; });
  });
})();
