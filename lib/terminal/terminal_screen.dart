import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:xterm/xterm.dart';

import 'terminal_links.dart';
import 'terminal_packs.dart';
import 'terminal_search.dart';
import 'terminal_session.dart';
import '../pentest/report.dart';
import '../pentest/target.dart';
import '../pentest/targets_screen.dart';
import '../pentest/targets_store.dart';
import '../power/battery_opt.dart';
import '../device/device_api.dart';
import '../storage/setup_storage.dart';
import '../agents/agents_screen.dart';

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

  /// Per-session listener closures.
  ///
  /// A shared tear-off cannot report WHICH session notified (ChangeNotifier
  /// hands the listener no sender), so every notification used to be read
  /// against the ACTIVE tab: a background tab whose shell died fired the
  /// listener, which inspected tab 1, found nothing wrong and swallowed the
  /// failure -- and no second notification ever arrived, so the user switched
  /// to a dead tab and found a frozen prompt with no explanation. A stale
  /// error flag could also be cleared off the WRONG session.
  ///
  /// ChangeNotifier.removeListener needs the same function object that was
  /// added, so each closure is kept here rather than rebuilt at removal.
  final Map<TerminalSession, VoidCallback> _sessionListeners = {};

  /// Sticky modifiers for the extra-keys bar (Termux-style).
  bool _ctrlHeld = false;
  bool _altHeld = false;

  /// Terminal font size, persisted across launches.
  static const _fontSizeKey = 'terminal_font_size';
  double _fontSize = 14;

  /// Terminal font + theme packs, persisted across launches.
  static const _fontPackKey = 'terminal_font_pack';
  static const _themePackKey = 'terminal_theme_pack';
  String _fontPack = 'system';
  String _themePack = 'interlux';

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
    SharedPreferences.getInstance().then((prefs) {
      final size = prefs.getDouble(_fontSizeKey);
      final font = prefs.getString(_fontPackKey);
      final theme = prefs.getString(_themePackKey);
      if (!mounted) return;
      setState(() {
        if (size != null) _fontSize = size.clamp(10.0, 24.0);
        if (font != null) _fontPack = fontPackById(font).id;
        if (theme != null) _themePack = themePackById(theme).id;
      });
    });
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
    _listen(session);
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
      _listen(fresh);
      final old = _sessions[0];
      _unlisten(old);
      setState(() {
        _sessions[0] = fresh;
        _activeIndex = 0;
      });
      old.dispose();
      fresh.boot();
      return;
    }
    final removed = _sessions[index];
    _unlisten(removed);
    setState(() {
      _sessions.removeAt(index);
      if (_activeIndex >= _sessions.length) {
        _activeIndex = _sessions.length - 1;
      }
    });
    removed.dispose();
  }

  /// Attach a listener bound to one specific session (see _sessionListeners).
  void _listen(TerminalSession session) {
    void listener() => _onSessionChanged(session);
    _sessionListeners[session] = listener;
    session.addListener(listener);
  }

  void _unlisten(TerminalSession session) {
    final listener = _sessionListeners.remove(session);
    if (listener != null) session.removeListener(listener);
  }

  void _onSessionChanged(TerminalSession session) {
    // Reports on the session that actually notified, not on whichever tab
    // happens to be active: see _sessionListeners.
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
    // The share sheet can fail to present (nothing handles text/plain, or the
    // sheet is dismissed). Left as a bare call the rejection became an
    // unhandled async error and the handler died with no feedback at all.
    shareSessionReport(
      terminal: session.terminal,
      sessionName: session.name,
      targetLabel: session.targetLabel,
      command: session.command,
    ).catchError((Object error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Could not share this session.'),
          duration: Duration(seconds: 2),
        ),
      );
    });
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

  void _openAgents() {
    Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => const AgentsScreen()),
    );
  }

  /// Paste the clipboard into the active shell.
  ///
  /// Uses terminal.paste(), not textInput(): a shell that has advertised
  /// bracketed paste mode receives the text wrapped in the paste escape
  /// sequence, so a multi-line paste lands on the prompt as editable literal
  /// text instead of every newline executing the next line the moment it
  /// arrives. A shell with no bracketed paste support (plain /system/bin/sh)
  /// gets plain input -- the same path the keyboard already takes.
  Future<void> _pasteFromClipboard() async {
    final text = await DeviceApi.clipboardGet();
    if (!mounted) return;
    if (text == null || text.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Nothing to paste — the clipboard is empty.'),
          duration: Duration(seconds: 2),
        ),
      );
      return;
    }
    _active.terminal.paste(text);
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

  Future<void> _changeFontSize(double size) async {
    final clamped = size.clamp(10.0, 24.0);
    setState(() => _fontSize = clamped);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setDouble(_fontSizeKey, clamped);
  }

  Future<void> _changeFontPack(String id) async {
    final pack = fontPackById(id);
    setState(() => _fontPack = pack.id);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_fontPackKey, pack.id);
  }

  Future<void> _changeThemePack(String id) async {
    final pack = themePackById(id);
    setState(() => _themePack = pack.id);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_themePackKey, pack.id);
  }

  void _openAppearance() {
    showDialog(
      context: context,
      builder: (context) => _AppearanceDialog(
        size: _fontSize,
        fontPack: _fontPack,
        themePack: _themePack,
        onSizeChanged: _changeFontSize,
        onFontChanged: _changeFontPack,
        onThemeChanged: _changeThemePack,
      ),
    );
  }

  /// A tap on a link offers to open it (confirm dialog, never auto-launch).
  /// Any other tap does nothing here — focus and keyboard stay with the view.
  void _tapLink(TapUpDetails details, CellOffset at) {
    final url = findLinkAt(_active.terminal, at);
    if (url == null || !mounted) return;
    // Capture the screen's messenger BEFORE the dialog exists. The builder
    // parameter used to shadow this State's `context`, so the snackbar below
    // was looked up through the DIALOG's context -- which is unmounted the
    // moment Navigator.pop runs. The message therefore vanished with the
    // dialog, which is exactly the "no browser found" case it exists to
    // report: the user got zero feedback. The builder parameter is renamed
    // so the shadowing cannot come back.
    final messenger = ScaffoldMessenger.of(context);
    showDialog(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: const Color(0xFF1A1A1A),
        title: const Text(
          'Open link?',
          style: TextStyle(color: Color(0xFFE6E6E6), fontSize: 16),
        ),
        content: Text(
          url,
          style: const TextStyle(color: Color(0xFF55CDCD), fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: url));
              Navigator.pop(dialogContext);
            },
            child: const Text('Copy'),
          ),
          TextButton(
            onPressed: () async {
              Navigator.pop(dialogContext);
              final ok = await DeviceApi.openUrl(url);
              if (!ok && mounted) {
                messenger.showSnackBar(
                  const SnackBar(
                    content: Text('No browser found for this link.'),
                  ),
                );
              }
            },
            child: const Text('Open'),
          ),
        ],
      ),
    );
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
      _unlisten(session);
      session.dispose();
    }
    // Created here and previously never released -- a genuine undisposed
    // disposable that leak_tracker flags in debug builds. It dies with the
    // screen so the impact is small, but it should not leak.
    _targetsStore.dispose();
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
                onAgents: _openAgents,
                onShare: _shareActiveReport,
                onSearch: _openSearch,
                onTextSize: _openAppearance,
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
                onTapUp: (details, at) => _tapLink(details, at),
                theme: themePackById(_themePack).theme,
                textStyle: TerminalStyle(
                  fontSize: _fontSize,
                  fontFamily: fontPackById(_fontPack).family,
                ),
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
        onPaste: () {
          _pasteFromClipboard();
        },
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
  final VoidCallback onAgents;
  final VoidCallback onShare;
  final VoidCallback onSearch;
  final VoidCallback onTextSize;

  const _SessionTabBar({
    required this.sessions,
    required this.activeIndex,
    required this.onSelect,
    required this.onClose,
    required this.onAdd,
    required this.onTargets,
    required this.onAgents,
    required this.onShare,
    required this.onSearch,
    required this.onTextSize,
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
            onTap: onAgents,
            child: Container(
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              margin: const EdgeInsets.all(4),
              decoration: BoxDecoration(
                color: const Color(0xFF232323),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Icon(
                Icons.smart_toy,
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
          GestureDetector(
            onTap: onTextSize,
            child: Container(
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(horizontal: 12),
              margin: const EdgeInsets.all(4),
              decoration: BoxDecoration(
                color: const Color(0xFF232323),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Text(
                'Aa',
                style: TextStyle(color: Color(0xFFE6E6E6), fontSize: 14),
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

/// Text-size dialog: slider 10–24sp with live preview, persisted.
/// Terminal appearance: text size slider + font pack + theme pack.
/// All three persist via SharedPreferences (see [_TerminalScreenState]).
class _AppearanceDialog extends StatelessWidget {
  final double size;
  final String fontPack;
  final String themePack;
  final ValueChanged<double> onSizeChanged;
  final ValueChanged<String> onFontChanged;
  final ValueChanged<String> onThemeChanged;

  const _AppearanceDialog({
    required this.size,
    required this.fontPack,
    required this.themePack,
    required this.onSizeChanged,
    required this.onFontChanged,
    required this.onThemeChanged,
  });

  @override
  Widget build(BuildContext context) {
    const label = TextStyle(color: Color(0xFF999999), fontSize: 13);
    return AlertDialog(
      backgroundColor: const Color(0xFF1A1A1A),
      title: const Text(
        'Appearance',
        style: TextStyle(color: Color(0xFFE6E6E6), fontSize: 16),
      ),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'interlux:/\$ echo Aa',
              style: TextStyle(
                color: const Color(0xFFE6E6E6),
                fontSize: size,
                fontFamily: fontPackById(fontPack).family,
              ),
            ),
            Slider(
              value: size,
              min: 10,
              max: 24,
              divisions: 14,
              label: size.toStringAsFixed(0),
              onChanged: onSizeChanged,
            ),
            const Text('Font', style: label),
            for (final p in terminalFontPacks)
              _PackOption(
                label: p.label,
                selected: p.id == fontPack,
                onTap: () {
                  onFontChanged(p.id);
                  Navigator.pop(context);
                },
              ),
            const SizedBox(height: 8),
            const Text('Theme', style: label),
            for (final p in terminalThemePacks)
              _PackOption(
                label: p.label,
                selected: p.id == themePack,
                swatch: p.theme.background,
                onTap: () {
                  onThemeChanged(p.id);
                  Navigator.pop(context);
                },
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Done'),
        ),
      ],
    );
  }
}

/// One selectable row in the appearance dialog: label + checkmark,
/// optional color swatch preview; tap applies and closes.
class _PackOption extends StatelessWidget {
  final String label;
  final bool selected;
  final Color? swatch;
  final VoidCallback onTap;

  const _PackOption({
    required this.label,
    required this.selected,
    required this.onTap,
    this.swatch,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
        child: Row(
          children: [
            if (swatch != null)
              Container(
                width: 16,
                height: 16,
                margin: const EdgeInsets.only(right: 8),
                decoration: BoxDecoration(
                  color: swatch,
                  borderRadius: BorderRadius.circular(4),
                  border: Border.all(color: const Color(0xFF444444)),
                ),
              ),
            Expanded(
              child: Text(
                label,
                style: const TextStyle(
                    color: Color(0xFFE6E6E6), fontSize: 14),
              ),
            ),
            if (selected)
              const Text(
                '✓',
                style: TextStyle(color: Color(0xFF55CC55), fontSize: 14),
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
  final VoidCallback onPaste;

  const _ExtraKeysBar({
    required this.ctrlHeld,
    required this.altHeld,
    required this.onToggleCtrl,
    required this.onToggleAlt,
    required this.onKey,
    required this.onPaste,
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
          // Paste first: it is the one action a phone keyboard gives you no
          // key for, so it must be reachable without scrolling the row.
          _KeyButton(label: 'PASTE', onTap: onPaste),
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
