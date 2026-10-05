import 'dart:async';
import 'dart:convert';

import 'package:flutter/services.dart';

/// Talks to the native pty bridge over platform channels.
///
/// One instance owns one shell session: `start` returns a native session id
/// and output arrives on a dedicated channel (`interlux/pty/events/<id>`).
/// Sessions are fully independent — opening, closing, or stalling one tab
/// can never affect another's stream.
///
/// Bytes coming back from the shell are decoded as UTF-8 before reaching the
/// terminal emulator; a partial multi-byte sequence at the edge of a read is
/// buffered until the rest arrives, so wide characters never corrupt across
/// chunk boundaries.
class PtyService {
  static const _method = MethodChannel('interlux/pty');

  final _output = StreamController<String>.broadcast();
  final _exited = StreamController<void>.broadcast();

  /// Decoded output from the shell.
  Stream<String> get output => _output.stream;

  /// Fires when the shell closes the pty (EOF).
  Stream<void> get exited => _exited.stream;

  /// Native session id from `start`. Null until booted.
  int? _sessionId;

  bool _running = false;
  bool get isRunning => _running;

  /// Set once dispose() has run. start() re-checks it after its await:
  /// dispose() used to run while the native 'start' call was still in flight,
  /// find _sessionId still null, and stop nothing -- then start() resumed and
  /// marked a disposed service as running, leaking a native pty that ran for
  /// the life of the process with no owner, no UI and no way to close it.
  bool _disposed = false;

  StreamSubscription? _subscription;

  Future<void> start() async {
    if (_running) return;
    final id = await _method.invokeMethod<int>('start');
    if (id == null || id < 0) {
      throw PlatformException(
        code: 'PTY_CREATE_FAILED',
        message: 'native session id missing',
      );
    }
    if (_disposed) {
      // dispose() ran while the call above was in flight and could not stop
      // anything, because the id did not exist yet. Take the session back
      // down rather than reviving a disposed service.
      try {
        await _method.invokeMethod('stop', {'id': id});
      } on PlatformException {
        // The pty may already be gone; that is fine.
      }
      return;
    }
    _sessionId = id;
    _running = true;

    final events = EventChannel('interlux/pty/events/$id');
    _subscription = events.receiveBroadcastStream().listen(
      (data) {
        // A late event arriving after dispose() must not touch a closed
        // controller (see dispose).
        if (_disposed) return;
        if (data is Uint8List) {
          _handleBytes(data);
        } else if (data is String) {
          _output.add(data);
        }
      },
      onError: (error) {
        if (_disposed) return;
        _running = false;
        _exited.add(null);
      },
      onDone: () {
        if (_disposed) return;
        _running = false;
        _exited.add(null);
      },
    );
  }

  // A UTF-8 sequence can be split across two reads from the kernel; hold the
  // tail here until it completes.
  final List<int> _pending = [];

  void _handleBytes(Uint8List bytes) {
    _pending.addAll(bytes);
    try {
      final decoded = utf8.decode(_pending, allowMalformed: false);
      _pending.clear();
      if (decoded.isNotEmpty) _output.add(decoded);
    } on FormatException {
      // Truncated sequence — wait for the next read.
      // Drop only if the buffer grows unreasonably, which would mean corrupt
      // data rather than a split codepoint.
      if (_pending.length > 64) {
        _output.add(utf8.decode(_pending, allowMalformed: true));
        _pending.clear();
      }
    }
  }

  Future<void> write(String input) async {
    if (!_running) return;
    final id = _sessionId;
    if (id == null) return;
    await _method.invokeMethod('write', {'id': id, 'data': utf8.encode(input)});
  }

  Future<void> resize(int cols, int rows) async {
    final id = _sessionId;
    if (id == null) return;
    await _method.invokeMethod('resize', {
      'id': id,
      'cols': cols,
      'rows': rows,
    });
  }

  Future<void> stop() async {
    _running = false;
    await _subscription?.cancel();
    _subscription = null;
    final id = _sessionId;
    _sessionId = null;
    if (id == null) return;
    try {
      await _method.invokeMethod('stop', {'id': id});
    } on PlatformException {
      // The pty may already be gone; that is fine.
    }
  }

  void dispose() {
    _disposed = true;
    // Close the controllers only once stop() has actually torn the
    // subscription down. Closing them first meant an event landing between
    // the two called add() on a closed controller -- "Bad state: Cannot add
    // new events after calling close", thrown from a stream callback as an
    // unhandled zone error rather than anything the caller could catch.
    stop().whenComplete(() {
      _output.close();
      _exited.close();
    });
  }
}
