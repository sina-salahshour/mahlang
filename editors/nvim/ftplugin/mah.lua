-- Mah language server integration for Neovim.
--
-- This file is installed to `<nvim-config>/ftplugin/mah.lua` by `make
-- install-lsp`. It starts the Mah language server for every `.mh` buffer.
--
-- The server ships as part of the `mah` CLI (`mah lsp`), installed on PATH
-- by `make install-cli`, so we just look for that command rather than
-- locating any server script or Python interpreter ourselves.

-- Mah uses `#` for line comments. Set these so built-in commenting (gcc / the
-- `vim.lsp.buf.code_action` comment toggle) and any commenting plugin work
-- without a "commentstring is not defined" / "option not set" error.
vim.bo.commentstring = "# %s"
vim.bo.comments = ":#"

if vim.fn.executable("mah") ~= 1 then
  vim.notify(
    "[mah] `mah` command not found on PATH." ..
      "\nRun `make install-cli` from the mahlang repository, then restart Neovim.",
    vim.log.levels.WARN
  )
  return
end

-- Anchor the workspace at the nearest project marker, falling back to the
-- file's directory.
local bufname = vim.api.nvim_buf_get_name(0)
local root_markers = vim.fs.find(
  { "mah.lang", ".git", "Makefile" },
  { upward = true, path = vim.fs.dirname(bufname) }
)
local root_dir = root_markers[1] and vim.fs.dirname(root_markers[1])
  or vim.fs.dirname(bufname)

local client_id = vim.lsp.start({
  name = "mah-lsp",
  cmd = { "mah", "lsp" },
  root_dir = root_dir,
})

-- Turn on autocomplete-on-type where the Neovim build supports it (0.11+).
-- This makes the completion menu pop up automatically as you type instead of
-- requiring a manual <C-x><C-o>.
if client_id and vim.lsp.completion and vim.lsp.completion.enable then
  local ok = pcall(vim.lsp.completion.enable, true, client_id, 0, {
    autotrigger = true,
  })
  if not ok then
    -- Older signature / partial support: fall back to a manual enable.
    pcall(vim.lsp.completion.enable, true, client_id, 0)
  end
end
