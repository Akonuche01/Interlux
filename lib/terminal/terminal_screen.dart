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
    );
  }
}
