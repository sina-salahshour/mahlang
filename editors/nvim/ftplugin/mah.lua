-- Mah language server integration for Neovim.
--
-- This file is installed to `<nvim-config>/ftplugin/mah.lua` by `make
-- install-lsp`. It starts the Mah language server for every `.mh` buffer.
--
-- The server bundle is installed next to the Neovim config as
-- `<nvim-config>/mah-lsp/`, so we locate it relative to this script rather
-- than hard-coding an absolute path.

local function script_path()
  local info = debug.getinfo(1, "S")
  return info.source:sub(2) -- strip the leading "@"
end

-- <nvim-config>/ftplugin/mah.lua -> <nvim-config>
local ftplugin_dir = vim.fs.dirname(script_path())
local nvim_dir = vim.fs.dirname(ftplugin_dir)
local server_script = nvim_dir .. "/mah-lsp/lsp/server.py"

if vim.fn.filereadable(server_script) ~= 1 then
  vim.notify(
    "[mah] language server not found at " .. server_script ..
      "\nRun `make install-lsp` from the mahlang repository.",
    vim.log.levels.WARN
  )
  return
end

-- Pick a Python interpreter: honour $MAH_LSP_PYTHON, else python3, else python.
local function find_python()
  local override = vim.env.MAH_LSP_PYTHON
  if override and #override > 0 then
    return override
  end
  if vim.fn.executable("python3") == 1 then
    return "python3"
  end
  if vim.fn.executable("python") == 1 then
    return "python"
  end
  return nil
end

local python = find_python()
if not python then
  vim.notify("[mah] no python interpreter found for the language server", vim.log.levels.ERROR)
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

vim.lsp.start({
  name = "mah-lsp",
  cmd = { python, server_script },
  root_dir = root_dir,
})
