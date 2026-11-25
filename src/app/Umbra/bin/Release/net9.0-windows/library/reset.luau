-- name: respawn
-- desc: resets your character
local char = game:GetService("Players").LocalPlayer.Character
local humanoid = char and char:FindFirstChildOfClass("Humanoid")
if humanoid then humanoid.Health = 0 end
