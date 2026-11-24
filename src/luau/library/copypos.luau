-- name: copy position
-- desc: puts your xyz on the clipboard as a teleport-ready string
local char = game:GetService("Players").LocalPlayer.Character
local root = char and char:FindFirstChild("HumanoidRootPart")
if not root then return end
local p = root.Position
local text = string.format("%.1f, %.1f, %.1f", p.X, p.Y, p.Z)
if setclipboard then setclipboard(text) end
print("position: " .. text)
