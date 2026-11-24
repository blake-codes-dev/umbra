-- name: super jump
-- desc: jumppower 200, survives respawn
local Players = game:GetService("Players")
local player = Players.LocalPlayer

local function set(char)
    local humanoid = char:WaitForChild("Humanoid", 5)
    if humanoid then humanoid.JumpPower = 200 end
end

if player.Character then set(player.Character) end
player.CharacterAdded:Connect(set)
