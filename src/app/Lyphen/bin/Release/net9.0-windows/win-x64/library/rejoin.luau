-- name: rejoin
-- desc: comes back to the same place. needs the executor
local placeId = game.PlaceId
if queue_on_teleport then queue_on_teleport("print('rejoined')") end
if game:GetService("TeleportService") then
    pcall(function()
        game:GetService("TeleportService"):Teleport(placeId)
    end)
end
