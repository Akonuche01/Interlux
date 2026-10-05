import 'package:flutter/widgets.dart';
import 'package:xterm/xterm.dart';

/// Terminal font packs: bundled monospace TTFs (see pubspec `fonts:`)
/// plus the platform monospace. Applied through [TerminalStyle.fontFamily].
class TerminalFontPack {
  final String id;
  final String label;
  final String family;

  const TerminalFontPack({
    required this.id,
    required this.label,
    required this.family,
  });
}

const List<TerminalFontPack> terminalFontPacks = [
  TerminalFontPack(id: 'system', label: 'System', family: 'monospace'),
  TerminalFontPack(
      id: 'jetbrains', label: 'JetBrains Mono', family: 'JetBrainsMono'),
  TerminalFontPack(id: 'fira', label: 'Fira Code', family: 'FiraCode'),
];

TerminalFontPack fontPackById(String id) {
  for (final p in terminalFontPacks) {
    if (p.id == id) return p;
  }
  return terminalFontPacks.first;
}

/// Terminal theme packs: named [TerminalTheme]s (16 ANSI colors +
/// cursor/selection/foreground/background). `interlux` is the standing
/// default shipped since v1; the rest are classic schemes.
class TerminalThemePack {
  final String id;
  final String label;
  final TerminalTheme theme;

  const TerminalThemePack({
    required this.id,
    required this.label,
    required this.theme,
  });
}

const List<TerminalThemePack> terminalThemePacks = [
  TerminalThemePack(
    id: 'interlux',
    label: 'Interlux',
    theme: TerminalTheme(
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
  ),
  TerminalThemePack(
    id: 'dracula',
    label: 'Dracula',
    theme: TerminalTheme(
      cursor: Color(0xFFF8F8F2),
      selection: Color(0xFF44475A),
      foreground: Color(0xFFF8F8F2),
      background: Color(0xFF282A36),
      black: Color(0xFF21222C),
      white: Color(0xFFF8F8F2),
      red: Color(0xFFFF5555),
      green: Color(0xFF50FA7B),
      yellow: Color(0xFFF1FA8C),
      blue: Color(0xFFBD93F9),
      magenta: Color(0xFFFF79C6),
      cyan: Color(0xFF8BE9FD),
      brightBlack: Color(0xFF6272A4),
      brightRed: Color(0xFFFF6E6E),
      brightGreen: Color(0xFF69FF94),
      brightYellow: Color(0xFFFFFFA5),
      brightBlue: Color(0xFFD6ACFF),
      brightMagenta: Color(0xFFFF92DF),
      brightCyan: Color(0xFFA4FFFF),
      brightWhite: Color(0xFFFFFFFF),
      searchHitBackground: Color(0xFFFFB86C),
      searchHitBackgroundCurrent: Color(0xFFFF5E5E),
      searchHitForeground: Color(0xFF000000),
    ),
  ),
  TerminalThemePack(
    id: 'solarized',
    label: 'Solarized Dark',
    theme: TerminalTheme(
      cursor: Color(0xFF839496),
      selection: Color(0xFF073642),
      foreground: Color(0xFF839496),
      background: Color(0xFF002B36),
      black: Color(0xFF073642),
      white: Color(0xFFEEE8D5),
      red: Color(0xFFDC322F),
      green: Color(0xFF859900),
      yellow: Color(0xFFB58900),
      blue: Color(0xFF268BD2),
      magenta: Color(0xFFD33682),
      cyan: Color(0xFF2AA198),
      brightBlack: Color(0xFF002B36),
      brightRed: Color(0xFFCB4B16),
      brightGreen: Color(0xFF586E75),
      brightYellow: Color(0xFF657B83),
      brightBlue: Color(0xFF839496),
      brightMagenta: Color(0xFF6C71C4),
      brightCyan: Color(0xFF93A1A1),
      brightWhite: Color(0xFFFDF6E3),
      searchHitBackground: Color(0xFFFFB86C),
      searchHitBackgroundCurrent: Color(0xFFFF5E5E),
      searchHitForeground: Color(0xFF000000),
    ),
  ),
  TerminalThemePack(
    id: 'onedark',
    label: 'One Dark',
    theme: TerminalTheme(
      cursor: Color(0xFF528BFF),
      selection: Color(0xFF3E4451),
      foreground: Color(0xFFABB2BF),
      background: Color(0xFF282C34),
      black: Color(0xFF282C34),
      white: Color(0xFFABB2BF),
      red: Color(0xFFE06C75),
      green: Color(0xFF98C379),
      yellow: Color(0xFFE5C07B),
      blue: Color(0xFF61AFEF),
      magenta: Color(0xFFC678DD),
      cyan: Color(0xFF56B6C2),
      brightBlack: Color(0xFF545862),
      brightRed: Color(0xFFE06C75),
      brightGreen: Color(0xFF98C379),
      brightYellow: Color(0xFFE5C07B),
      brightBlue: Color(0xFF61AFEF),
      brightMagenta: Color(0xFFC678DD),
      brightCyan: Color(0xFF56B6C2),
      brightWhite: Color(0xFFFFFFFF),
      searchHitBackground: Color(0xFFFFB86C),
      searchHitBackgroundCurrent: Color(0xFFFF5E5E),
      searchHitForeground: Color(0xFF000000),
    ),
  ),
  TerminalThemePack(
    id: 'whiteblack',
    label: 'White on Black',
    theme: TerminalTheme(
      cursor: Color(0xFFAEAFAD),
      selection: Color(0xFFAEAFAD),
      foreground: Color(0xFFE5E5E5),
      background: Color(0xFF000000),
      black: Color(0xFF000000),
      white: Color(0xFFE5E5E5),
      red: Color(0xFFCD3131),
      green: Color(0xFF0DBC79),
      yellow: Color(0xFFE5E510),
      blue: Color(0xFF2472C8),
      magenta: Color(0xFFBC3FBC),
      cyan: Color(0xFF11A8CD),
      brightBlack: Color(0xFF666666),
      brightRed: Color(0xFFF14C4C),
      brightGreen: Color(0xFF23D18B),
      brightYellow: Color(0xFFF5F543),
      brightBlue: Color(0xFF3B8EEA),
      brightMagenta: Color(0xFFD670D6),
      brightCyan: Color(0xFF29B8DB),
      brightWhite: Color(0xFFE5E5E5),
      searchHitBackground: Color(0xFFFFB86C),
      searchHitBackgroundCurrent: Color(0xFFFF5E5E),
      searchHitForeground: Color(0xFF000000),
    ),
  ),
];

TerminalThemePack themePackById(String id) {
  for (final p in terminalThemePacks) {
    if (p.id == id) return p;
  }
  return terminalThemePacks.first;
}
