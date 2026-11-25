-- name: walk speed
-- desc: sets your walkspeed to 100 and keeps it through respawns
local Players = game:GetService("Players")
local player = Players.LocalPlayer

local function set(char)
    local humanoid = char:WaitForChild("Humanoid", 5)
    if humanoid then humanoid.WalkSpeed = 100 end
end

if player.Character then set(player.Character) end
player.CharacterAdded:Connect(set)
