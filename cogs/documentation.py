"""
Documentation lookup commands.
"""
import re
import io
import aiohttp
import discord
from html import unescape
from discord.ext import commands
from discord.ext.commands import Context

ALLOWED_CHANNEL = 1313786489112494080
SECTIONS = ["guides", "guild", "major-changes"]


def slugify(text: str) -> str:
    """Mimic astro formatting."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    text = re.sub(r"-+", "-", text)
    return text


def html_to_markdown(html: str) -> str:
    """Convert a small subset of HTML to Markdown preserving formatting."""
    # This was plucked from somewhere and is barely tested. Hopefully it works.
    if not html:
        return ""

    s = html

    # Normalize newlines
    s = s.replace('\r\n', '\n').replace('\r', '\n')

    # Pre/code blocks
    def _repl_pre(m):
        inner = m.group(1)
        # Strip surrounding <code> if any
        inner = re.sub(r"^\s*<code[^>]*>(.*)</code>\s*$", r"\1", inner, flags=re.I | re.S)
        return "\n```\n" + inner.strip() + "\n```\n"

    s = re.sub(r"<pre[^>]*>(.*?)</pre>", _repl_pre, s, flags=re.I | re.S)

    # Inline code
    s = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: f"`{m.group(1).strip()}`", s, flags=re.I | re.S)

    # Headings
    for i in range(1, 7):
        s = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", lambda m, i=i: "\n" + ("#" * i) + " " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n\n", s, flags=re.I | re.S)

    # Links
    s = re.sub(r"<a[^>]*href=[\'\"](.*?)[\'\"][^>]*>(.*?)</a>", lambda m: f"[{re.sub(r'<[^>]+>', '', m.group(2)).strip()}]({m.group(1)})", s, flags=re.I | re.S)

    # Images
    s = re.sub(r"<img[^>]*src=[\'\"](.*?)[\'\"][^>]*alt=[\'\"](.*?)[\'\"][^>]*>", lambda m: f"![{m.group(2)}]({m.group(1)})", s, flags=re.I | re.S)
    s = re.sub(r"<img[^>]*src=[\'\"](.*?)[\'\"][^>]*>", lambda m: f"![]({m.group(1)})", s, flags=re.I | re.S)

    # Bold / strong
    s = re.sub(r"<(b|strong)[^>]*>(.*?)</\1>", lambda m: f"**{re.sub(r'<[^>]+>', '', m.group(2)).strip()}**", s, flags=re.I | re.S)

    # Italic / em
    s = re.sub(r"<(i|em)[^>]*>(.*?)</\1>", lambda m: f"*{re.sub(r'<[^>]+>', '', m.group(2)).strip()}*", s, flags=re.I | re.S)

    # Blockquotes
    s = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", lambda m: "\n" + "\n".join(["> " + re.sub(r'<[^>]+>', '', line).strip() for line in m.group(1).strip().splitlines()]) + "\n\n", s, flags=re.I | re.S)

    # Lists
    def _li_to_dash(m):
        items = re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), flags=re.I | re.S)
        out = []
        for it in items:
            it_text = re.sub(r"<[^>]+>", "", it).strip()
            out.append("- " + it_text)
        return "\n" + "\n".join(out) + "\n\n"

    s = re.sub(r"<ul[^>]*>(.*?)</ul>", _li_to_dash, s, flags=re.I | re.S)

    # Ordered lists (simple)
    def _ol_to_num(m):
        items = re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), flags=re.I | re.S)
        out = []
        for idx, it in enumerate(items, 1):
            it_text = re.sub(r"<[^>]+>", "", it).strip()
            out.append(f"{idx}. " + it_text)
        return "\n" + "\n".join(out) + "\n\n"

    s = re.sub(r"<ol[^>]*>(.*?)</ol>", _ol_to_num, s, flags=re.I | re.S)

    # Paragraphs -> double newline
    s = re.sub(r"<p[^>]*>(.*?)</p>", lambda m: "\n" + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n\n", s, flags=re.I | re.S)

    # Remove remaining tags
    s = re.sub(r"<[^>]+>", "", s)

    # Unescape HTML entities
    s = unescape(s)

    # Normalize whitespace and trim
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class Documentation(commands.Cog, name="documentation"): 
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="docs", description="Fetch docs from wynnvets.org")
    async def docs(self, ctx: Context, topic: str, *, subject: str = None) -> None:
        """
        Fetch documentation for a given topic and an optional heading.

        Usage: docs <topic> [heading]
        The command will try all doc sections; at the time of implementation these are `guides`, `guild`, then `major-changes`.
        """
        # Allow this to be used in the bot commands channel, or by staff
        has_delete_perm = getattr(ctx.author, "guild_permissions", None)
        if not (ctx.channel.id == ALLOWED_CHANNEL or (has_delete_perm and has_delete_perm.manage_messages)):
            await ctx.send("You don't have permission to use this command here.")
            return

        # Prepare subject slug
        slug = slugify(subject) if subject else None

        # This is probably a terrible implementation.
        async with aiohttp.ClientSession() as session:
            found = False
            for section in SECTIONS:
                url = f"https://wynnvets.org/docs/{section}/{topic}/"
                try:
                    async with session.get(url) as resp:
                        if resp.status == 404:
                            # Try next section
                            continue
                        if resp.status != 200:
                            await ctx.send(f"Error fetching a doc (HTTP {resp.status}) for {section}/{topic}.")
                            return
                        text = await resp.text()
                except Exception as exc:
                    await ctx.send(f"Error: {exc}")
                    return

                # If a subject slug is given, try to extract that anchored section
                if slug:
                    # Why did I decide to do this...
                    # Shit regex to match <h1..h6 ... id="slug" ...>Heading</hX> followed by content until next <h[1-6]
                    pattern = re.compile(
                        rf"(<h[1-6][^>]*id=['\"]{re.escape(slug)}['\"][^>]*>.*?</h[1-6]>)(.*?)(?=<h[1-6]|$)",
                        re.I | re.S,
                    )
                    m = pattern.search(text)
                    if m:
                        heading_html = m.group(1)
                        content_html = m.group(2)
                        # Convert HTML to Markdown while preserving formatting
                        combined = heading_html + "\n" + content_html
                        # Trim everything above the "Published <date>" header if present
                        pub_m = re.search(r"Published\s*.*?\d{4}", combined, re.I | re.S)
                        if pub_m:
                            combined = combined[pub_m.start():]
                        md = html_to_markdown(combined).strip()
                        if not md:
                            md = "(No text found.)"

                        title = f"{section}/{topic}#{slug}"
                        await self._send_content(ctx, title, md)
                        found = True
                        break
                    else:
                        continue
                else:
                    # No title = use top
                    title_m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
                    if title_m:
                        page_title = unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
                    else:
                        h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.I | re.S)
                        page_title = unescape(re.sub(r"<[^>]+>", "", h1_m.group(1))).strip() if h1_m else f"{section}/{topic}"

                    # Get first 800 characters (discord limit)
                    body_m = re.search(r"<body[^>]*>(.*?)</body>", text, re.I | re.S)
                    body = body_m.group(1) if body_m else text
                    # So much shit to get rid of.
                    body = re.sub(r"<script.*?>.*?</script>", "", body, flags=re.I | re.S)
                    body = re.sub(r"<style.*?>.*?</style>", "", body, flags=re.I | re.S)
                    # Trim everything above the "Published <date>" header.
                    pub_m = re.search(r"Published\s*.*?\d{4}", body, re.I | re.S)
                    if pub_m:
                        body = body[pub_m.start():]
                    # Limit to paragraphs, assuming I didn't write spans on the article. I may have.
                    p_m = re.search(r"<p[^>]*>(.*?)</p>", body, re.I | re.S)
                    if p_m:
                        excerpt = html_to_markdown(p_m.group(1)).strip()
                    else:
                        # Fallback: if I did, this might work
                        cleaned_body = html_to_markdown(body).strip()
                        excerpt = cleaned_body[:800]

                    if not excerpt:
                        excerpt = "(???)"

                    title = f"{section}/{topic}"
                    await self._send_content(ctx, title, excerpt)
                    found = True
                    break

            if not found:
                await ctx.send("You probably typo'd the slug.")

    async def _send_content(self, ctx: Context, title: str, content: str) -> None:
        """If short enough, embed, otherwise put this as a file."""
        # Normal
        max_embed = 4000
        if len(content) <= max_embed:
            embed = discord.Embed(title=title, description=content, color=discord.Color.blurple())
            await ctx.send(embed=embed)
            return

        # This is stupid
        file_bytes = content.encode("utf-8")
        file_obj = io.BytesIO(file_bytes)
        file_obj.seek(0)
        filename = f"{title.replace('/', '_')}.md"
        discord_file = discord.File(fp=file_obj, filename=filename)
        await ctx.send(content=f"Output too long; attached as a markdown file: {filename}", file=discord_file)


async def setup(bot) -> None:
    await bot.add_cog(Documentation(bot))
