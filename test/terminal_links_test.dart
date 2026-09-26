import 'package:flutter_test/flutter_test.dart';
import 'package:interlux/terminal/terminal_links.dart';
import 'package:xterm/xterm.dart';

void main() {
  group('findLinkAt', () {
    test('finds http link under the tap', () {
      final term = Terminal(maxLines: 100);
      term.write('see https://example.com/a here\n');
      // 'https' starts at column 4.
      final hit = findLinkAt(term, const CellOffset(10, 0));
      expect(hit, 'https://example.com/a');
    });

    test('miss outside any link', () {
      final term = Terminal(maxLines: 100);
      term.write('see https://example.com/a here\n');
      expect(findLinkAt(term, const CellOffset(0, 0)), isNull);
      expect(findLinkAt(term, const CellOffset(30, 0)), isNull);
    });

    test('rejects non-http schemes', () {
      final term = Terminal(maxLines: 100);
      term.write('x javascript:alert(1)\n');
      // Tapping the word itself must not produce an openable link.
      expect(findLinkAt(term, const CellOffset(4, 0)), isNull);
    });

    test('strips trailing punctuation', () {
      final term = Terminal(maxLines: 100);
      term.write('go https://example.com/a.\n');
      expect(
        findLinkAt(term, const CellOffset(6, 0)),
        'https://example.com/a',
      );
    });

    test('out-of-range cells return null without throwing', () {
      final term = Terminal(maxLines: 100);
      term.write('hi\n');
      expect(findLinkAt(term, const CellOffset(99, 0)), isNull);
      expect(findLinkAt(term, const CellOffset(0, 9999)), isNull);
    });
  });
}
