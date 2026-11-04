/* Formery Timesheet Database - vanilla JavaScript (no libraries).
   Everything here is a progressive enhancement: the site works without it and
   all validation is repeated on the server. */
(function () {
  "use strict";

  // ---- Mobile navigation ---------------------------------------------------
  var toggle = document.querySelector(".nav-toggle");
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }
  if (toggle) {
    toggle.addEventListener("click", function () {
      setNav(!document.body.classList.contains("nav-open"));
    });
  }
  document.querySelectorAll("[data-nav-close]").forEach(function (el) {
    el.addEventListener("click", function () { setNav(false); });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { setNav(false); }
  });

  // ---- Dismissable flash messages ------------------------------------------
  document.querySelectorAll(".flash-close").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var flash = btn.closest(".flash");
      if (flash) { flash.remove(); }
    });
  });
  document.querySelectorAll(".flash-success").forEach(function (flash) {
    setTimeout(function () { if (flash.parentNode) { flash.remove(); } }, 8000);
  });

  // ---- Confirmation before sensitive POST actions --------------------------
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // ---- Chart bars: animate in ---------------------------------------------
  document.querySelectorAll("[data-chart] .bar-fill").forEach(function (bar) {
    var target = bar.getAttribute("data-height");
    if (target === null) { return; }
    bar.style.height = "0%";
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { bar.style.height = target + "%"; });
    });
  });

  // ---- Timesheet form -------------------------------------------------------
  var form = document.querySelector(".timesheet-form[data-standard-hours]");
  if (!form) { return; }

  var standardHours = parseFloat(form.getAttribute("data-standard-hours")) || 8;
  var timeIn = form.querySelector("[name=time_in]");
  var timeOut = form.querySelector("[name=time_out]");
  var breakMin = form.querySelector("[name=break_minutes]");
  var regular = form.querySelector("[name=regular_hours]");
  var overtime = form.querySelector("[name=overtime_hours]");
  var note = form.querySelector("[data-calc-note]");
  var modes = form.querySelectorAll("[name=hours_mode]");

  function currentMode() {
    var checked = form.querySelector("[name=hours_mode]:checked");
    return checked ? checked.value : "manual";
  }
  function toMinutes(value) {
    if (!value) { return null; }
    var parts = value.split(":");
    return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
  }
  function round2(n) { return Math.round(n * 100) / 100; }

  // Mirrors the server rules in timesheets/hours.py (the server is authoritative).
  function compute() {
    var a = toMinutes(timeIn && timeIn.value);
    var b = toMinutes(timeOut && timeOut.value);
    if (a === null || b === null) { return { state: "missing" }; }
    var shift = ((b - a) % 1440 + 1440) % 1440;
    if (shift === 0) { return { state: "same" }; }
    var brk = parseInt(breakMin && breakMin.value, 10) || 0;
    var net = shift - brk;
    if (net <= 0) { return { state: "break" }; }
    var total = round2(net / 60);
    return {
      state: "ok", total: total, overnight: b < a,
      regular: round2(Math.min(total, standardHours)),
      overtime: round2(Math.max(total - standardHours, 0))
    };
  }

  function say(text, level) {
    if (!note) { return; }
    note.textContent = text;
    note.className = "hours-note" + (level ? " " + level : "");
  }

  function refresh() {
    var mode = currentMode();
    var result = compute();
    var calculating = mode === "calculate";

    [regular, overtime].forEach(function (input) {
      if (!input) { return; }
      input.readOnly = calculating;
      input.classList.toggle("is-locked", calculating);
    });

    if (calculating) {
      if (result.state === "ok") {
        regular.value = result.regular.toFixed(2);
        overtime.value = result.overtime.toFixed(2);
        say("Calculated: " + result.total.toFixed(2) + " h total (" + result.regular.toFixed(2) +
            " regular + " + result.overtime.toFixed(2) + " overtime)" +
            (result.overnight ? ". Overnight shift: time out is on the following day." : "."), "");
      } else if (result.state === "same") {
        say("Time out must be different from time in.", "bad");
      } else if (result.state === "break") {
        say("The unpaid break must be shorter than the shift.", "bad");
      } else {
        say("Enter time in and time out to calculate hours.", "warn");
      }
    } else if (result.state === "ok") {
      var typed = round2((parseFloat(regular.value) || 0) + (parseFloat(overtime.value) || 0));
      if (typed > 0 && Math.abs(typed - result.total) > 0.005) {
        say("Manual entry: the times above imply " + result.total.toFixed(2) +
            " h, but you entered " + typed.toFixed(2) + " h. Your manual figures will be saved as typed.", "warn");
      } else {
        say("", "");
      }
    } else {
      say("", "");
    }
  }

  modes.forEach(function (radio) { radio.addEventListener("change", refresh); });
  [timeIn, timeOut, breakMin, regular, overtime].forEach(function (input) {
    if (input) { input.addEventListener("input", refresh); }
  });
  refresh();

  // ---- Project task checklist (ticked tasks are written into the description) --
  var projectSelect = form.querySelector("[name=project]");
  var taskBox = document.getElementById("task-checklist");
  var taskItems = document.getElementById("task-checklist-items");
  var tasksEl = document.getElementById("project-tasks-map");
  var descField = form.querySelector("[name=task_description]");
  if (projectSelect && taskBox && taskItems && tasksEl && descField) {
    var tasksMap = {};
    try { tasksMap = JSON.parse(tasksEl.textContent); } catch (e) { tasksMap = {}; }
    var lineFor = function (name) { return "- " + name; };
    var lines = function () { return descField.value.split("\n"); };
    var renderTasks = function () {
      var names = tasksMap[projectSelect.value] || [];
      taskItems.textContent = "";
      taskBox.hidden = names.length === 0;
      var current = lines();
      names.forEach(function (name, i) {
        var id = "task-check-" + i;
        var label = document.createElement("label");
        label.className = "task-check";
        label.setAttribute("for", id);
        var box = document.createElement("input");
        box.type = "checkbox";
        box.id = id;
        box.checked = current.indexOf(lineFor(name)) !== -1;
        box.addEventListener("change", function () {
          var rest = lines().filter(function (l) { return l !== lineFor(name); });
          if (box.checked) { rest.push(lineFor(name)); }
          descField.value = rest.join("\n").replace(/^\n+/, "");
        });
        label.appendChild(box);
        label.appendChild(document.createTextNode(" " + name));
        taskItems.appendChild(label);
      });
    };
    projectSelect.addEventListener("change", renderTasks);
    renderTasks();
  }

  // ---- Admin form: show the selected employee's ID ------------------------
  var employeeSelect = form.querySelector("[name=employee]");
  var codeInput = document.getElementById("employee-code");
  var mapEl = document.getElementById("employee-code-map");
  if (employeeSelect && codeInput && mapEl) {
    var map = {};
    try { map = JSON.parse(mapEl.textContent); } catch (e) { map = {}; }
    var showCode = function () { codeInput.value = map[employeeSelect.value] || ""; };
    employeeSelect.addEventListener("change", showCode);
    showCode();
  }
})();
