"""A small VT100/xterm screen emulator, enough for NetHack's tty port (standard library only)."""
import codecs

COLORS = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")
DEC_GRAPHICS = dict(zip("`afgjklmnopqrstuvwxyz{|}~",
                        "◆▒°±┘┐┌└┼⎺⎻─⎼⎽"
                        "├┤┴┬│≤≥π≠£·"))


class VT:
    """A small VT100/xterm screen: enough of the protocol for NetHack's tty port."""

    def __init__(self, rows=24, cols=80):
        self.rows, self.cols = rows, cols
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.reset()

    def reset(self):
        self.chars = [[" "] * self.cols for _ in range(self.rows)]
        self.fg = [["default"] * self.cols for _ in range(self.rows)]
        self.bold = [[False] * self.cols for _ in range(self.rows)]
        self.rev = [[False] * self.cols for _ in range(self.rows)]
        self.y = self.x = 0
        self.attr = ("default", False, False)
        self.top, self.bottom = 0, self.rows - 1
        self.wrap = False
        self.saved = (0, 0, self.attr)
        self.dec = False
        self.esc = ""
        self.complete = False
        # Rows written or erased since the caller last cleared this set; an unchanged row may still hold old text.
        self.touched = set(range(self.rows))

    def feed(self, data):
        for ch in self.decoder.decode(data):
            if self.esc:
                self.esc += ch
                self._escape()
            elif ch == "\x1b":
                self.esc = ch
            elif ord(ch) < 32 or ch == "\x7f":
                self._control(ch)
            else:
                self._put(ch)

    def lines(self):
        return ["".join(row) for row in self.chars]

    def _put(self, ch):
        if self.dec:
            ch = DEC_GRAPHICS.get(ch, ch)
        if self.wrap:
            self.x, self.wrap = 0, False
            self._linefeed()
        self.chars[self.y][self.x] = ch
        self.touched.add(self.y)
        self.fg[self.y][self.x], self.bold[self.y][self.x], self.rev[self.y][self.x] = self.attr
        if self.x == self.cols - 1:
            self.wrap = True
        else:
            self.x += 1

    def _control(self, ch):
        if ch == "\r":
            self.x, self.wrap = 0, False
        elif ch in "\n\x0b\x0c":
            self.wrap = False
            self._linefeed()
        elif ch == "\b":
            self.x, self.wrap = max(0, self.x - 1), False
        elif ch == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
        elif ch == "\x0e":
            self.dec = True
        elif ch == "\x0f":
            self.dec = False

    def _linefeed(self):
        if self.y == self.bottom:
            self._scroll(self.top, self.bottom, 1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _blank(self, y, x0, x1):
        self.touched.add(y)
        for x in range(max(0, x0), min(self.cols, x1)):
            self.chars[y][x], self.fg[y][x], self.bold[y][x], self.rev[y][x] = " ", "default", False, False

    def _scroll(self, top, bottom, n):
        """Scroll rows top..bottom up by n (down if n < 0)."""
        self.touched.update(range(top, bottom + 1))
        for grid in (self.chars, self.fg, self.bold, self.rev):
            blank = {id(self.chars): " ", id(self.fg): "default"}.get(id(grid), False)
            block = grid[top:bottom + 1]
            k = min(abs(n), len(block))
            if n > 0:
                block = block[k:] + [[blank] * self.cols for _ in range(k)]
            else:
                block = [[blank] * self.cols for _ in range(k)] + block[:len(block) - k]
            grid[top:bottom + 1] = block

    def _escape(self):
        s = self.esc
        if len(s) == 2:
            c = s[1]
            if c in "[()#%":
                return
            if c == "7":
                self.saved = (self.y, self.x, self.attr)
            elif c == "8":
                self.y, self.x, self.attr = self.saved
            elif c == "M":
                if self.y == self.top:
                    self._scroll(self.top, self.bottom, -1)
                else:
                    self.y = max(0, self.y - 1)
            elif c == "D":
                self._linefeed()
            elif c == "E":
                self.x = 0
                self._linefeed()
            elif c == "c":
                self.reset()
                self.complete = True
            self.esc = ""
            return
        if s[1] in "()#%":
            if s[1] == "(":
                self.dec = s[2] == "0"
            self.esc = ""
            return
        final = s[-1]
        if "@" <= final <= "~":
            self.esc = ""
            self._csi(s[2:-1], final)
        elif len(s) > 64:
            self.esc = ""

    def _csi(self, params, final):
        private = params[:1] in ("?", ">", "=", "!")
        nums = []
        for p in (params.lstrip("?>=!").split(";") if params else []):
            nums.append(int(p) if p.isdigit() else None)

        def num(i, default=1):
            v = nums[i] if i < len(nums) and nums[i] is not None else default
            return default if v == 0 and default == 1 else v

        self.wrap = False
        if final in "Hf":
            self.y, self.x = min(self.rows - 1, num(0) - 1), min(self.cols - 1, num(1) - 1)
        elif final == "A":
            self.y = max(0, self.y - num(0))
        elif final == "B":
            self.y = min(self.rows - 1, self.y + num(0))
        elif final == "C":
            self.x = min(self.cols - 1, self.x + num(0))
        elif final == "D":
            self.x = max(0, self.x - num(0))
        elif final == "E":
            self.y, self.x = min(self.rows - 1, self.y + num(0)), 0
        elif final == "F":
            self.y, self.x = max(0, self.y - num(0)), 0
        elif final == "G" or final == "`":
            self.x = min(self.cols - 1, num(0) - 1)
        elif final == "d":
            self.y = min(self.rows - 1, num(0) - 1)
        elif final == "J":
            how = num(0, 0)
            if how == 0:
                self._blank(self.y, self.x, self.cols)
                for y in range(self.y + 1, self.rows):
                    self._blank(y, 0, self.cols)
                if self.y == 0 and self.x == 0:
                    self.complete = True
            elif how == 1:
                for y in range(self.y):
                    self._blank(y, 0, self.cols)
                self._blank(self.y, 0, self.x + 1)
            else:
                for y in range(self.rows):
                    self._blank(y, 0, self.cols)
                self.complete = True
        elif final == "K":
            how = num(0, 0)
            if how == 0:
                self._blank(self.y, self.x, self.cols)
            elif how == 1:
                self._blank(self.y, 0, self.x + 1)
            else:
                self._blank(self.y, 0, self.cols)
        elif final == "m" and not private:
            self._sgr(nums or [0])
        elif final == "r" and not private:
            top, bottom = num(0) - 1, (nums[1] if len(nums) > 1 and nums[1] else self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
            self.y = self.x = 0
        elif final in "LM" and self.top <= self.y <= self.bottom:
            self._scroll(self.y, self.bottom, -num(0) if final == "L" else num(0))
        elif final == "S":
            self._scroll(self.top, self.bottom, num(0))
        elif final == "T":
            self._scroll(self.top, self.bottom, -num(0))
        elif final == "P":
            n, row = num(0), self.y
            self.touched.add(row)
            for grid, blank in ((self.chars, " "), (self.fg, "default"), (self.bold, False), (self.rev, False)):
                line = grid[row]
                grid[row] = line[:self.x] + line[self.x + n:] + [blank] * min(n, self.cols - self.x)
                grid[row] = grid[row][:self.cols]
        elif final == "@":
            n, row = num(0), self.y
            self.touched.add(row)
            for grid, blank in ((self.chars, " "), (self.fg, "default"), (self.bold, False), (self.rev, False)):
                line = grid[row]
                grid[row] = (line[:self.x] + [blank] * n + line[self.x:])[:self.cols]
        elif final == "X":
            self._blank(self.y, self.x, self.x + num(0))
        elif final in "hl" and private and 1049 in nums and final == "h":
            for y in range(self.rows):
                self._blank(y, 0, self.cols)

    def _sgr(self, nums):
        fg, bold, rev = self.attr
        i = 0
        while i < len(nums):
            n = nums[i] or 0
            if n == 0:
                fg, bold, rev = "default", False, False
            elif n == 1:
                bold = True
            elif n == 22:
                bold = False
            elif n == 7:
                rev = True
            elif n == 27:
                rev = False
            elif 30 <= n <= 37:
                fg = COLORS[n - 30]
            elif n == 39:
                fg = "default"
            elif 90 <= n <= 97:
                fg = "bright" + COLORS[n - 90]
            elif n in (38, 48):
                i += 2 if i + 1 < len(nums) and nums[i + 1] == 5 else 4
            i += 1
        self.attr = (fg, bold, rev)
