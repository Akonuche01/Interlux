import 'dart:async';

import 'package:flutter/material.dart';
import 'package:xterm/xterm.dart';

import 'pty_service.dart';

/// One terminal session: emulator + shell pipe + view controller.
///
/// Each tab in [TerminalScreen] owns a session. Sessions boot independently
/// (their own pty + shell) and dispose cleanly when their tab closes.
class TerminalSession extends ChangeNotifier {
  static int _nextId = 1;

  /// Short tab label, e.g. "t1".
  final String name;

  final Terminal terminal = Terminal(maxLines: 5000);
  final PtyService pty = PtyService();
  final TerminalController controller = TerminalController();

  StreamSubscription<String>? _outputSub;
  StreamSubscription<void>? _exitedSub;

  /// True once the shell has exited (tab shows dead state).
  bool exited = false;

  /// Last boot failure, if any (shown as a snackbar by the screen).
  Object? error;

  TerminalSession() : name = 't${_nextId++}' {
    terminal.onOutput = (data) {
      pty.write(data);
    };
  }

  /// Start the shell and wire output. Safe to call once.
  Future<void> boot() async {
    try {
      await pty.start();
    } catch (e) {
      error = e;
      notifyListeners();
      return;
    }

    _outputSub = pty.output.listen((data) {
      terminal.write(data);
    });

    _exitedSub = pty.exited.listen((_) {
      exited = true;
      notifyListeners();
    });

    // Size the pty to whatever grid the emulator settled on.
    terminal.resize(
      terminal.viewWidth.isFinite && terminal.viewWidth > 0
          ? terminal.viewWidth
          : 80,
      terminal.viewHeight.isFinite && terminal.viewHeight > 0
          ? terminal.viewHeight
          : 24,
    );
    pty.resize(terminal.viewWidth, terminal.viewHeight);
  }

  @override
  void dispose() {
    _outputSub?.cancel();
    _exitedSub?.cancel();
    controller.dispose();
    pty.dispose();
    super.dispose();
  }
}
