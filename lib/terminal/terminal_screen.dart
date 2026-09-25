import 'package:flutter/material.dart';
import 'package:xterm/xterm.dart';

import 'terminal_search.dart';
import 'terminal_session.dart';
import '../pentest/report.dart';
import '../pentest/target.dart';
import '../pentest/targets_screen.dart';
import '../pentest/targets_store.dart';
import '../power/battery_opt.dart';
import '../device/device_api.dart';
import '../storage/setup_storage.dart';

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
  final List<TerminalSession> _sessions = [];
  int _activeIndex = 0;
  final TargetsStore _targetsStore = TargetsStore();

  /// Sticky modifiers for the extra-keys bar (Termux-style).
  bool _ctrlHeld = false;
  bool _altHeld = false;

  /// Scrollback search state (per screen, always on the active tab).
  bool _searchOpen = false;
  final TextEditingController _searchField = TextEditingController();
  final FocusNode _searchFocus = FocusNode();
  List<TerminalMatch> _matches = const [];
  int _matchIndex = 0;

  TerminalSession get _active => _sessions[_activeIndex];

  @override
  void initState() {
    super.initState();
    _targetsStore.load();
    _addSession();
    // Onboarding prompts run in sequence so dialogs never stack.
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      await BatteryOptPrompt.maybeShow(context);
      if (mounted) await StoragePrompt.maybeShow(context);
    });
  }

  void _addSession({String? run, String? targetLabel}) {
    final session = TerminalSession()
      ..targetLabel = targetLabel
      ..command = run;
    session.addListener(_onSessionChanged);
    if (_searchOpen) _closeSearch();
    setState(() {
      _sessions.add(session);
      _activeIndex = _sessions.length - 1;
    });
    session.boot().then((_) {
      // The pty buffers early input, so auto-running a scan command here
      // is safe even if the shell hasn't printed its first prompt yet.
      if (run != null && run.isNotEmpty) session.pty.write('$run\n');
    });
  }

  void _closeSession(int index) {
    if (_searchOpen) _closeSearch();
    if (_sessions.length == 1) {
      // Never leave the user with no terminal: reset the last tab.
      final fresh = TerminalSession();
      fresh.addListener(_onSessionChanged);
      final old = _sessions[0];
      old.removeListener(_onSessionChanged);
      setState(() {
        _sessions[0] = fresh;
        _activeIndex = 0;
      });
      old.dispose();
      fresh.boot();
      return;
    }
    final removed = _sessions[index];
    removed.removeListener(_onSessionChanged);
    setState(() {
      _sessions.removeAt(index);
      if (_activeIndex >= _sessions.length) {
        _activeIndex = _sessions.length - 1;
      }
    });
    removed.dispose();
  }

  void _onSessionChanged() {
    final session = _sessions[_activeIndex];
    if (session.error != null) {
      _showError('Could not start the terminal: ${session.error}');
      session.error = null;
    } else if (session.exited) {
      _showError('The shell exited (${session.name}).');
      session.exited = false;
    }
    if (mounted) setState(() {});
  }

  void _openSearch() {
    // The terminal view holds autofocus; yield it or keystrokes keep going
    // to the shell instead of the search field.
    FocusScope.of(context).unfocus();
    setState(() {
      _searchOpen = true;
      _matches = const [];
      _matchIndex = 0;
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && _searchOpen) _searchFocus.requestFocus();
    });
  }

  void _closeSearch() {
    _searchFocus.unfocus();
    _active.controller.clearSelection();
    _searchField.clear();
    setState(() {
      _searchOpen = false;
      _matches = const [];
      _matchIndex = 0;
    });
  }

  /// Re-scan the active tab. Called on query change and every prev/next
  /// press, so hits never go stale as output arrives.
  void _refreshSearch() {
    final query = _searchField.text;
    if (!_searchOpen || query.isEmpty) {
      _active.controller.clearSelection();
      setState(() {
        _matches = const [];
        _matchIndex = 0;
      });
      return;
    }
    final matches = findTerminalMatches(_active.terminal, query);
    var index = _matchIndex;
    if (matches.isEmpty) {
      _active.controller.clearSelection();
      index = 0;
    } else {
      index = index.clamp(0, matches.length - 1);
      if (!highlightTerminalMatch(
        _active.terminal,
        _active.controller,
        matches[index],
      )) {
        // Buffer shifted under us; rescan once and take the nearest hit.
        final fresh = findTerminalMatches(_active.terminal, query);
        if (fresh.isEmpty) {
          _active.controller.clearSelection();
          setState(() {
            _matches = const [];
            _matchIndex = 0;
          });
          return;
        }
        index = index.clamp(0, fresh.length - 1);
        highlightTerminalMatch(
          _active.terminal,
          _active.controller,
          fresh[index],
        );
        setState(() {
          _matches = fresh;
          _matchIndex = index;
        });
        return;
      }
    }
    setState(() {
      _matches = matches;
      _matchIndex = index;
    });
  }

  void _stepSearch(int delta) {
    if (_matches.isEmpty) {
      _refreshSearch();
      return;
    }
    _matchIndex = (_matchIndex + delta) % _matches.length;
    if (_matchIndex < 0) _matchIndex += _matches.length;
    _refreshSearch();
  }

  void _shareActiveReport() {
    final session = _active;
    shareSessionReport(
      terminal: session.terminal,
      sessionName: session.name,
      targetLabel: session.targetLabel,
      command: session.command,
    );
  }

  void _openTargets() {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => TargetsScreen(
          store: _targetsStore,
          onLaunch: (PentestTarget target, String command) {
            _addSession(run: command, targetLabel: target.label);
            DeviceApi.notify('Scan started on ${target.label}', command);
            DeviceApi.vibrate(60);
          },
        ),
      ),
    );
  }

  void _sendExtraKey(_ExtraKey extra) {
    if (extra.text != null) {
      _active.pty.write(extra.text!);
    } else if (extra.key != null) {
      _active.terminal.keyInput(
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
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), duration: const Duration(days: 1)),
    );
  }

  @override
  void dispose() {
    _searchFocus.dispose();
    _searchField.dispose();
    for (final session in _sessions) {
      session.removeListener(_onSessionChanged);
      session.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final active = _active;
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Column(
          children: [
            _SessionTabBar(
              sessions: _sessions,
              activeIndex: _activeIndex,
              onSelect: (i) {
                if (_searchOpen) _closeSearch();
                setState(() => _activeIndex = i);
              },
              onClose: _closeSession,
              onAdd: _addSession,
              onTargets: _openTargets,
              onShare: _shareActiveReport,
              onSearch: _openSearch,
            ),
            if (_searchOpen) _SearchBar(
              field: _searchField,
              matchText: _matches.isEmpty
                  ? (_searchField.text.isEmpty ? '' : '0/0')
                  : '${_matchIndex + 1}/${_matches.length}',
              onChanged: (_) => _refreshSearch(),
              onPrev: () => _stepSearch(-1),
              onNext: () => _stepSearch(1),
              onClose: _closeSearch,
            ),
            Expanded(
              child: TerminalView(
                active.terminal,
                controller: active.controller,
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
          // visiblePassword disables IME composing/autocorrect: keystrokes
          // commit immediately so the cursor tracks typed text instead of
          // sitting left of an underlined composing preview. Autocorrect
          // would corrupt shell input anyway.
          keyboardType: TextInputType.visiblePassword,
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

/// Tab strip above the terminal: one chip per session plus a + button.
/// Long-press (or the ×) closes a tab; closing the last tab resets it.
class _SessionTabBar extends StatelessWidget {
  final List<TerminalSession> sessions;
  final int activeIndex;
  final void Function(int index) onSelect;
  final void Function(int index) onClose;
  final VoidCallback onAdd;
  final VoidCallback onTargets;
  final VoidCallback onShare;
  final VoidCallback onSearch;

  const _SessionTabBar({
    required this.sessions,
    required this.activeIndex,
    required this.onSelect,
    required this.onClose,
    required this.onAdd,
    required this.onTargets,
    required this.onShare,
    required this.onSearch,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF111111),
      height: 40,
      child: ListView(
        scrollDirection: Axis.horizontal,
        children: [
          for (var i = 0; i < sessions.length; i++)
            GestureDetector(
              onTap: () => onSelect(i),
              onLongPress: () => onClose(i),
              child: Container(
                alignment: Alignment.center,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                margin: const EdgeInsets.all(4),
                decoration: BoxDecoration(
                  color: i == activeIndex
                      ? const Color(0xFF3A2E5C)
                      : const Color(0xFF232323),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      sessions[i].name,
                      style: const TextStyle(
                        color: Color(0xFFE6E6E6),
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(width: 6),
                    GestureDetector(
                      onTap: () => onClose(i),
                      child: const Icon(
                        Icons.close,
                        size: 14,
                        color: Color(0xFF999999),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          GestureDetector(
            onTap: onAdd,
            child: Container(
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              margin: const EdgeInsets.all(4),
              decoration: BoxDecoration(
                color: const Color(0xFF232323),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Icon(
                Icons.add,
                size: 16,
                color: Color(0xFFE6E6E6),
              ),
            ),
          ),
          GestureDetector(
            onTap: onTargets,
            child: Container(
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              margin: const EdgeInsets.all(4),
              decoration: BoxDecoration(
                color: const Color(0xFF232323),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Icon(
                Icons.radar,
                size: 16,
                color: Color(0xFFE6E6E6),
              ),
            ),
          ),
          GestureDetector(
            onTap: onShare,
            child: Container(
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              margin: const EdgeInsets.all(4),
              decoration: BoxDecoration(
                color: const Color(0xFF232323),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Icon(
                Icons.share,
                size: 16,
                color: Color(0xFFE6E6E6),
              ),
            ),
          ),
          GestureDetector(
            onTap: onSearch,
            child: Container(
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              margin: const EdgeInsets.all(4),
              decoration: BoxDecoration(
                color: const Color(0xFF232323),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Icon(
                Icons.search,
                size: 16,
                color: Color(0xFFE6E6E6),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Scrollback search bar: query field, i/N counter, prev/next, close.
///
/// Matches highlight through the terminal selection; the bar sits above the
/// terminal so output stays visible while searching.
class _SearchBar extends StatelessWidget {
  final TextEditingController field;
  final String matchText;
  final ValueChanged<String> onChanged;
  final VoidCallback onPrev;
  final VoidCallback onNext;
  final VoidCallback onClose;

  const _SearchBar({
    required this.field,
    required this.matchText,
    required this.onChanged,
    required this.onPrev,
    required this.onNext,
    required this.onClose,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF111111),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      child: Row(
        children: [
          Expanded(
            child: TextField(
              controller: field,
              autofocus: true,
              onChanged: onChanged,
              onSubmitted: (_) => onNext(),
              style: const TextStyle(color: Color(0xFFE6E6E6), fontSize: 14),
              decoration: const InputDecoration(
                hintText: 'Search scrollback',
                hintStyle: TextStyle(color: Color(0xFF777777), fontSize: 14),
                isDense: true,
                border: InputBorder.none,
              ),
            ),
          ),
          Text(
            matchText,
            style: const TextStyle(color: Color(0xFF999999), fontSize: 13),
          ),
          IconButton(
            onPressed: onPrev,
            icon: const Icon(Icons.arrow_upward, size: 18),
            color: const Color(0xFFE6E6E6),
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(),
          ),
          IconButton(
            onPressed: onNext,
            icon: const Icon(Icons.arrow_downward, size: 18),
            color: const Color(0xFFE6E6E6),
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(),
          ),
          IconButton(
            onPressed: onClose,
            icon: const Icon(Icons.close, size: 18),
            color: const Color(0xFFE6E6E6),
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(),
          ),
        ],
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
