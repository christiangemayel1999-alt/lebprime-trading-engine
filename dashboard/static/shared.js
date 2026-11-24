/* Shared display-only helpers. No trading controls. */
(function (root) {
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
    function time(value) {
        if (!value) return "Not available";
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) return esc(value);
        const seconds = Math.round((date.getTime() - Date.now()) / 1000);
        const unit = Math.abs(seconds) < 60 ? "second" : Math.abs(seconds) < 3600 ? "minute" : Math.abs(seconds) < 86400 ? "hour" : "day";
        const divisor = {second:1, minute:60, hour:3600, day:86400}[unit];
        const age = new Intl.RelativeTimeFormat(undefined, {numeric:"auto"}).format(Math.round(seconds/divisor), unit);
        return `<time datetime="${esc(date.toISOString())}" title="${esc(date.toUTCString())}">${esc(date.toLocaleString())}<span class="time-age">${esc(age)}</span></time>`;
    }
    function cards(container) {
        container.querySelectorAll("table").forEach(table => {
            const headers = Array.from(table.querySelectorAll("tr:first-child th"), h => h.textContent.trim());
            if (!headers.length) return;
            table.classList.add("mobile-cards");
            table.querySelectorAll("tr").forEach(row => {
                const cells = Array.from(row.children).filter(n => n.tagName === "TD");
                if (cells.length !== headers.length) return;
                cells.forEach((cell, i) => {
                    cell.dataset.label = headers[i];
                    cell.classList.toggle("card-secondary", i > 0 && !/symbol|side|result|p.?&.?l|net r|time|opened|closed|setting|health/i.test(headers[i]));
                });
                if (cells.length > 5 && !row.querySelector(".card-details")) {
                    const button = document.createElement("button");
                    button.type = "button"; button.className = "card-details";
                    button.textContent = "Details"; button.setAttribute("aria-expanded", "false");
                    button.onclick = event => {
                        event.stopPropagation();
                        const expanded = row.classList.toggle("card-expanded");
                        button.setAttribute("aria-expanded", String(expanded));
                        button.textContent = expanded ? "Less detail" : "Details";
                    };
                    cells[0].append(button);
                }
            });
        });
    }
    root.LebPrimeUI = {esc, time, cards};
    if (typeof module !== "undefined") module.exports = root.LebPrimeUI;
    if (typeof document === "undefined") return;
    document.addEventListener("submit", async event => {
        if (!event.target.matches("[data-logout]")) return;
        event.preventDefault();
        const button = event.target.querySelector("button"); button.disabled = true;
        try {
            const response = await fetch("/auth/logout", {method:"POST", credentials:"same-origin"});
            if (!response.ok) throw new Error("Logout unavailable. Access this dashboard through its authenticated gateway.");
            root.location.assign("/auth/login");
        } catch (error) { button.disabled = false; root.alert(error.message); }
    });
})(typeof window === "undefined" ? globalThis : window);
