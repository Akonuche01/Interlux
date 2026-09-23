import 'dart:async';

import 'package:flutter/material.dart';
import 'package:xterm/xterm.dart';

import 'pty_service.dart';

/// A full-screen interactive terminal backed by a real shell.
///
/// The [Terminal] emulator owns the screen state; [PtyService] is the pipe to
/// /system/bin/sh. Wiring is bidirectional:
///
///   keystroke -> terminal.onOutput -> pty.write   (input to shell)
///   shell read -> pty.output      -> terminal.write (output to screen)
///
/// Sizing also flows both ways: the emulator reports its grid size so the pty
/// gets a SIGWINCH via TIOCSWINSZ, and a fallback keeps the grid sane until
/// layout settles.
class TerminalScreen extends StatefulWidget {
  const TerminalScreen({super.key});

  @override
  State<TerminalScreen> createState() => _TerminalScreenState();
}

class _TerminalScreenState extends State<TerminalScreen> {
  late final Terminal _terminal;
  final PtyService _pty = PtyService();
  final TerminalController _controller = TerminalController();

  StreamSubscription<String>? _outputSub;
  StreamSubscription<void>? _exitedSub;
  bool _errorShown = false;

  /// Sticky modifiers for the extra-keys bar (Termux-style).
  bool _ctrlHeld = false;
  bool _altHeld = false;

  static const _fallbackCols = 80;
  static const _fallbackRows = 24;

  @override
  void initState() {
    super.initState();

    _terminal = Terminal(maxLines: 5000);

    // Keystrokes and pasted text go to the shell.
    _terminal.onOutput = (data) {
      _pty.write(data);
    };

    _boot();
  }

  Future<void> _boot() async {
    // Start the shell, then size it to the grid the emulator settled on.
    try {
      await _pty.start();
    } catch (e) {
      _showError('Could not start the terminal: $e');
      return;
    }

    _outputSub = _pty.output.listen((data) {
      _terminal.write(data);
    });

    _exitedSub = _pty.exited.listen((_) {
      if (mounted && !_errorShown) {
        _showError('The shell exited.');
      }
    });

    _terminal.resize(
      _terminal.viewWidth.isFinite && _terminal.viewWidth > 0
          ? _terminal.viewWidth
          : _fallbackCols,
      _terminal.viewHeight.isFinite && _terminal.viewHeight > 0
          ? _terminal.viewHeight
          : _fallbackRows,
    );
    _pty.resize(_terminal.viewWidth, _terminal.viewHeight);
  }

  void _sendExtraKey(_ExtraKey extra) {
    if (extra.text != null) {
      _pty.write(extra.text!);
    } else if (extra.key != null) {
      _terminal.keyInput(
        extra.key!,
        ctrl: extra.ctrlCombo || _ctrlHeld,
        alt: _altHeld,
      );
    }
    // Sticky modifiers are one-shot: a plain key consumes them, but a
    // dedicated Ctrl+C/D/Z combo leaves them armed for the next key.
    if (!extra.ctrlCombo && (_ctrlHeld || _altHeld)) {
      setState(() {
        _ctrlHeld = false;
        _altHeld = false;
      });
    }
  }

  void _showError(String message) {
    _errorShown = true;
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), duration: const Duration(days: 1)),
    );
  }

  @override
  void dispose() {
    _outputSub?.cancel();
    _exitedSub?.cancel();
    _controller.dispose();
    _pty.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: TerminalView(
                _terminal,
                controller: _controller,
                theme: const TerminalTheme(
            cursor: Color(0xFFE6E6E6),
            selection: Color(0x40E6E6E6),
            foreground: Color(0xFFE6E6E6),
            background: Color(0xFF000000),
            black: Color(0xFF000000),
            white: Color(0xFFE6E6E6),
            red: Color(0xFFCC5555),
            green: Color(0xFF55CC55),
            yellow: Color(0xFFCDCD55),
            blue: Color(0xFF5555CC),
            magenta: Color(0xFFCC55CC),
            cyan: Color(0xFF55CDCD),
            brightBlack: Color(0xFF666666),
            brightRed: Color(0xFFFF7777),
            brightGreen: Color(0xFF77FF77),
            brightYellow: Color(0xFFFFFF77),
            brightBlue: Color(0xFF7777FF),
            brightMagenta: Color(0xFFFF77FF),
            brightCyan: Color(0xFF77FFFF),
            brightWhite: Color(0xFFFFFFFF),
            searchHitBackground: Color(0xFFFFB86C),
            searchHitBackgroundCurrent: Color(0xFFFF5E5E),
            searchHitForeground: Color(0xFF000000),
          ),
          textStyle: const TerminalStyle(fontSize: 14),
          autofocus: true,
          hardwareKeyboardOnly: false,
          simulateScroll: true,
        ),
      ),
      _ExtraKeysBar(
        ctrlHeld: _ctrlHeld,
        altHeld: _altHeld,
        onToggleCtrl: () => setState(() => _ctrlHeld = !_ctrlHeld),
        onToggleAlt: () => setState(() => _altHeld = !_altHeld),
        onKey: _sendExtraKey,
      ),
    ],
  ),
  ),
);
}
}

/// One extra key: either a [TerminalKey] (arrows, esc, …) or raw [text].
class _ExtraKey {
  final String label;
  final TerminalKey? key;
  final String? text;
  final bool ctrlCombo;

  const _ExtraKey.key(this.label, this.key)
    : text = null,
      ctrlCombo = false;
  const _ExtraKey.text(this.label, this.text)
    : key = null,
      ctrlCombo = false;
  const _ExtraKey.ctrl(this.label, this.key) : text = null, ctrlCombo = true;
}

/// Termux-style extra-keys row: phone keyboards lack Esc/Tab/arrows/Ctrl,
/// all of which pentest tools (vim, nmap interactive, shells) need.
class _ExtraKeysBar extends StatelessWidget {
  final bool ctrlHeld;
  final bool altHeld;
  final VoidCallback onToggleCtrl;
  final VoidCallback onToggleAlt;
  final void Function(_ExtraKey key) onKey;

  const _ExtraKeysBar({
    required this.ctrlHeld,
    required this.altHeld,
    required this.onToggleCtrl,
    required this.onToggleAlt,
    required this.onKey,
  });

  static const _keys = [
    _ExtraKey.key('ESC', TerminalKey.escape),
    _ExtraKey.key('TAB', TerminalKey.tab),
    _ExtraKey.key('←', TerminalKey.arrowLeft),
    _ExtraKey.key('→', TerminalKey.arrowRight),
    _ExtraKey.key('↑', TerminalKey.arrowUp),
    _ExtraKey.key('↓', TerminalKey.arrowDown),
    _ExtraKey.key('HOME', TerminalKey.home),
    _ExtraKey.key('END', TerminalKey.end),
    _ExtraKey.key('DEL', TerminalKey.delete),
    _ExtraKey.key('PGUP', TerminalKey.pageUp),
    _ExtraKey.key('PGDN', TerminalKey.pageDown),
    _ExtraKey.text('|', '|'),
    _ExtraKey.text('/', '/'),
    _ExtraKey.text('-', '-'),
    _ExtraKey.text('~', '~'),
    _ExtraKey.ctrl('C', TerminalKey.keyC),
    _ExtraKey.ctrl('D', TerminalKey.keyD),
    _ExtraKey.ctrl('Z', TerminalKey.keyZ),
  ];

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF1A1A1A),
      height: 48,
      child: ListView(
        scrollDirection: Axis.horizontal,
        children: [
          _ToggleButton(
            label: 'CTRL',
            active: ctrlHeld,
            onTap: onToggleCtrl,
          ),
          _ToggleButton(label: 'ALT', active: altHeld, onTap: onToggleAlt),
          for (final k in _keys)
            _KeyButton(label: k.label, onTap: () => onKey(k)),
        ],
      ),
    );
  }
}

class _ToggleButton extends StatelessWidget {
  final String label;
  final bool active;
  final VoidCallback onTap;

  const _ToggleButton({
    required this.label,
    required this.active,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        alignment: Alignment.center,
        padding: const EdgeInsets.symmetric(horizontal: 14),
        margin: const EdgeInsets.all(4),
        decoration: BoxDecoration(
          color: active ? const Color(0xFF6B4FA1) : const Color(0xFF2A2A2A),
          borderRadius: BorderRadius.circular(6),
        ),
        child: Text(
          label,
          style: const TextStyle(color: Color(0xFFE6E6E6), fontSize: 13),
        ),
      ),
    );
  }
}

class _KeyButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;

  const _KeyButton({required this.label, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        alignment: Alignment.center,
        padding: const EdgeInsets.symmetric(horizontal: 14),
        margin: const EdgeInsets.all(4),
        decoration: BoxDecoration(
          color: const Color(0xFF2A2A2A),
          borderRadius: BorderRadius.circular(6),
        ),
        child: Text(
          label,
          style: const TextStyle(color: Color(0xFFE6E6E6), fontSize: 13),
        ),
      ),
    );
  }
}
