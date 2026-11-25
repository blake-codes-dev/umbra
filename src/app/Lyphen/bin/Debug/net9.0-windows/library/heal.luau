-- name: full heal
-- desc: refills your health on the spot
local char = game:GetService("Players").LocalPlayer.Character
local humanoid = char and char:FindFirstChildOfClass("Humanoid")
if humanoid then humanoid.Health = humanoid.MaxHealth end
