/* Local xterm 6.0.0 bridge. User data crosses QWebChannel, never JavaScript source. */
"use strict";
window.addEventListener("DOMContentLoaded", () => {
  new QWebChannel(qt.webChannelTransport, channel => {
    const bridge = channel.objects.bridge;
    const root = document.documentElement;
    const pagePalette = getComputedStyle(root);
    const term = new Terminal({fontFamily: "monospace", fontSize: 12, scrollback: 10000,
      cursorBlink: true, allowProposedApi: false, minimumContrastRatio: 4.5,
      theme: {background: pagePalette.backgroundColor, foreground: pagePalette.color}});
    const fit = new FitAddon.FitAddon();
    const search = new SearchAddon.SearchAddon();
    term.loadAddon(fit);
    term.loadAddon(search);
    // Consume OSC 52 even if a future xterm default starts supporting clipboard writes.
    term.parser.registerOscHandler(52, () => true);
    term.open(document.getElementById("terminal"));
    const encode = bytes => {
      let result = "";
      for (let i = 0; i < bytes.length; i += 8192) {
        result += String.fromCharCode(...bytes.subarray(i, i + 8192));
      }
      return btoa(result);
    };
    term.onData(text => bridge.input(encode(new TextEncoder().encode(text))));
    term.onBinary(text => bridge.input(btoa(text)));
    term.onResize(size => bridge.resize_terminal(size.cols, size.rows));
    term.onTitleChange(title => bridge.title_changed(title));
    term.textarea.addEventListener("focus", () => bridge.focus_changed(true));
    term.textarea.addEventListener("blur", () => bridge.focus_changed(false));
    term.attachCustomKeyEventHandler(event => {
      if (!event.ctrlKey || event.altKey || event.metaKey) return true;
      const key = event.key.toLowerCase();
      let action = null;
      if (event.shiftKey) {
        action = {c:"copy",v:"paste",f:"find",a:"select_all","+":"zoom_in","=":"zoom_in","-":"zoom_out","0":"zoom_reset"}[key];
      } else if (key === "c") action = "interrupt";
      else if (key === "z") action = "suspend";
      if (!action) return true;
      event.preventDefault();
      if (event.type === "keydown") {
        if (action === "copy") bridge.copy_selection(term.getSelection());
        else if (action === "select_all") term.selectAll();
        else bridge.key_action(action);
      }
      return false;
    });
    let snapshotTimer = null;
    const snapshot = () => {
      snapshotTimer = null;
      const lines = [];
      const buffer = term.buffer.active;
      for (let i = 0; i < buffer.length; i++) lines.push(buffer.getLine(i)?.translateToString(true) || "");
      bridge.snapshot(lines.join("\n"));
    };
    bridge.command.connect(encoded => {
      const msg = JSON.parse(encoded);
      switch (msg.op) {
        case "write": {
          const bytes = Uint8Array.from(atob(msg.data), ch => ch.charCodeAt(0));
          term.write(bytes, () => { if (snapshotTimer === null) snapshotTimer = setTimeout(snapshot, 120); });
          break;
        }
        case "paste": term.paste(msg.text); break;
        case "focus": term.focus(); break;
        case "fit": fit.fit(); break;
        case "clear": term.clear(); break;
        case "select_all": term.selectAll(); break;
        case "copy": bridge.copy_selection(term.getSelection()); break;
        case "scroll": term.scrollLines(msg.lines); break;
        case "settings":
          if (msg.theme) {
            root.style.colorScheme = msg.scheme;
            root.style.setProperty("--terminal-background", msg.theme.background);
            root.style.setProperty("--terminal-foreground", msg.theme.foreground);
            term.options.theme = msg.theme;
          }
          if (msg.family !== undefined &&
              (term.options.fontFamily !== msg.family || term.options.fontSize !== msg.size)) {
            term.options = {fontFamily: msg.family, fontSize: msg.size};
            fit.fit();
          }
          break;
        case "search":
          if (!msg.text) search.clearDecorations();
          else if (msg.previous) search.findPrevious(msg.text, {caseSensitive: msg.caseSensitive});
          else search.findNext(msg.text, {caseSensitive: msg.caseSensitive});
          break;
      }
    });
    new ResizeObserver(() => { if (document.body.clientWidth && document.body.clientHeight) fit.fit(); }).observe(document.body);
    bridge.ready();
    fit.fit();
  });
});
